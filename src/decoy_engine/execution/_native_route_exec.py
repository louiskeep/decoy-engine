"""Production entry point for the single-pass streaming native lane.

`try_native_route` is what `run_pipeline` (`_pipeline.py`) calls, right after
layer-1 FK routing and before `resolve_resident_sources` -- see
`_native_route.py`'s module docstring for why that placement is load-bearing.
It returns `(ExecutionResult, report)` when the whole single pass masked and
committed, `(None, report)` when the job reroutes to the ordinary oracle
continuation (the caller falls through unchanged; the original `LazySource`
was never exhausted, so `resolve_resident_sources` reads it fresh), and
raises once the pass is actually underway: everything past admission either
fully commits or raises -- there is no oracle fallback after that point.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._native_route import (
    LedgerEntry,
    NativeRouteLedger,
    NativeRouteReport,
    peek_and_admit,
    static_candidacy,
)
from decoy_engine.execution._native_route_preflight import (
    ExecutionDigestState,
    classify_and_preflight,
    run_widened_execution,
)
from decoy_engine.execution.native._kernels_scalar import (
    native_passthrough,
    native_redact,
    native_truncate,
)
from decoy_engine.instrumentation.timing import StrategyTimingRecord, rss_kb
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships import RelationshipGraph

# Batch size for the native lane's own read of the source. Independent of
# `chunk_size_rows` (that knob governs the UNRELATED auto-chunk oracle path);
# matches the out-of-core route's own default batch scale (`_planner`'s
# `AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT` neighborhood) so a fresh-process memory
# measurement is comparable across routes.
_NATIVE_BATCH_ROWS_DEFAULT = 50_000

_STREAMING_EXECUTION_MODE = "native"


def _rechain(
    first: pa.RecordBatch | None, rest: Iterator[pa.RecordBatch]
) -> Iterator[pa.RecordBatch]:
    # first=None only for a preflight-proved-empty widened table (0 batches).
    if first is not None:
        yield first
    yield from rest


def _resolve_truncate_keep(cfg: dict[str, Any]) -> str:
    """Mirror `TruncateHandler.run`'s legacy `from_end` -> `keep` resolution.

    A duplicate of `native._dispatch._resolve_truncate_keep`'s three lines
    rather than a cross-module import of that private helper: both read the
    identical, already-validated (`truncate_config_rejection` ran at
    admission) `keep`/`from_end` pair, so there is one behavior to keep in
    sync, not two.
    """
    keep = cfg.get("keep")
    if keep is not None:
        return keep
    return "tail" if bool(cfg.get("from_end", False)) else "head"


def _find_table(config: Mapping[str, Any], table: str) -> dict[str, Any] | None:
    for tbl in config.get("tables") or ():
        if isinstance(tbl, dict) and tbl.get("name") == table:
            return tbl
    return None


def _resolve_strategy_cfg(
    config: Mapping[str, Any], table: str, column_order: tuple[str, ...]
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Per-column (strategy, resolved kwargs), read once before the batch loop.

    Every name in `column_order` is guaranteed present in the table's
    `columns:` block by the admission precondition (`static_candidacy` +
    `peek_and_admit` already proved the batch schema equals the plan's
    declared columns, which come from this same config), so the lookup
    below cannot miss.
    """
    table_cfg = _find_table(config, table)
    by_name = {
        col.get("name"): col
        for col in (table_cfg or {}).get("columns") or ()
        if isinstance(col, dict)
    }
    resolved: dict[str, tuple[str, dict[str, Any]]] = {}
    for name in column_order:
        col = by_name[name]
        strategy = col["strategy"]
        provider_config = col.get("provider_config")
        cfg = provider_config if isinstance(provider_config, dict) else {}
        if strategy == "redact":
            kwargs: dict[str, Any] = {"redact_with": cfg.get("redact_with", "REDACTED")}
        elif strategy == "truncate":
            kwargs = {
                "length": cfg.get("length"),
                "keep": _resolve_truncate_keep(cfg),
                "mask_char": cfg.get("mask_char"),
            }
        else:
            kwargs = {}
        resolved[name] = (strategy, kwargs)
    return resolved


def _schema_drift_reason(expected: pa.Schema, actual: pa.Schema) -> str | None:
    """None when `actual` still matches the admitted first batch's schema."""
    expected_names, actual_names = set(expected.names), set(actual.names)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        return f"missing={missing};extra={extra}"
    for name in expected.names:
        want, got = expected.field(name).type, actual.field(name).type
        if want != got:
            return f"type_changed:{name}:{want}->{got}"
    return None


def _mask_one_batch(
    batch: pa.RecordBatch,
    chunk_index: int,
    *,
    table: str,
    column_order: tuple[str, ...],
    strategy_cfg: dict[str, tuple[str, dict[str, Any]]],
    out_schema: pa.Schema,
    ledger: NativeRouteLedger,
    timing_acc: dict[tuple[str, str], float],
    mem_acc: dict[tuple[str, str], int],
    boundary_ms_box: list[float],
) -> pa.RecordBatch:
    """Mask one batch column-by-column, updating the ledger and per-column
    timing/memory as each kernel call actually completes -- never in advance."""
    t_batch0 = time.perf_counter()
    arrays: list[pa.Array] = []
    col_time_total = 0.0
    n = batch.num_rows
    for name in column_order:
        strategy, kwargs = strategy_cfg[name]
        source = batch.column(name)
        ledger.native_attempted += 1
        ledger.native_rows_attempted += n
        rss_before = rss_kb()
        t0 = time.perf_counter()
        if strategy == "passthrough":
            arr = native_passthrough(source)
        elif strategy == "redact":
            arr = native_redact(source, redact_with=kwargs["redact_with"])
        else:  # "truncate", the only remaining allowlisted strategy
            arr = native_truncate(
                source, length=kwargs["length"], keep=kwargs["keep"], mask_char=kwargs["mask_char"]
            )
        elapsed = time.perf_counter() - t0
        # Same before/after RSS-delta bracket `timed_strategy` uses elsewhere
        # (`instrumentation/timing.py`); floored at zero because a negative
        # reading means the allocator gave memory back, not that the kernel
        # call itself shrank the process.
        delta_kb = max(0, rss_kb() - rss_before)
        col_time_total += elapsed
        key = (strategy, name)
        timing_acc[key] = timing_acc.get(key, 0.0) + elapsed * 1000.0
        mem_acc[key] = max(mem_acc.get(key, 0), delta_kb)
        arrays.append(arr)
        ledger.native_completed += 1
        ledger.native_rows_completed += n
        ledger.records.append(LedgerEntry(table=table, node=name, chunk_index=chunk_index))
    result_batch = pa.RecordBatch.from_arrays(arrays, schema=out_schema)
    # Read AFTER the Arrow construction above so the assembly/schema-binding
    # time this box measures actually includes the work it is named for --
    # reading it before `from_arrays` would silently exclude that call.
    batch_total = time.perf_counter() - t_batch0
    boundary_ms_box[0] += max(0.0, batch_total - col_time_total) * 1000.0
    return result_batch


def _validate_ledger(ledger: NativeRouteLedger, *, table: str) -> None:
    """Fail closed before any commit unless every frozen Part-1 invariant
    holds, checked independently so a corrupted single field can never hide
    behind another field that still looks fine:

    - attempted/completed CALLS match (a partial kernel failure the caller
      somehow swallowed would leave these apart);
    - attempted/completed ROWS match (the per-call counters could agree
      while the row counts a call actually claimed to process silently
      diverge, e.g. a batch mutated between the attempt and completion
      bump);
    - no chunk was rejected for schema drift (`_masked_batches` already
      raises before yielding a drifted chunk, so this is a second,
      independent guard against a ledger built by some other path);
    - no oracle/fallback call ever happened (there is no code path to one
      on this lane, so a non-zero count means something else corrupted the
      ledger object);
    - every record identifies THIS table, the record count matches the
      completed-call count, and no `(table, work-node, chunk index)`
      identity repeats -- the three-way proof that every native call that
      actually completed left exactly one, correctly-attributed trace.
    """
    if ledger.native_attempted != ledger.native_completed:
        raise ExecutionError(
            code="native_route_ledger_invalid",
            message=(
                f"{table!r}: native route ledger shows {ledger.native_attempted} attempted "
                f"vs {ledger.native_completed} completed calls; refusing to commit."
            ),
        )
    if ledger.native_rows_attempted != ledger.native_rows_completed:
        raise ExecutionError(
            code="native_route_ledger_invalid",
            message=(
                f"{table!r}: native route ledger shows {ledger.native_rows_attempted} rows "
                f"attempted vs {ledger.native_rows_completed} rows completed; refusing to commit."
            ),
        )
    if ledger.rejected_chunks:
        raise ExecutionError(
            code="native_route_ledger_invalid",
            message=(
                f"{table!r}: native route ledger shows {ledger.rejected_chunks} rejected "
                "chunk(s); refusing to commit."
            ),
        )
    if ledger.oracle_calls or ledger.oracle_rows or ledger.fallback_calls or ledger.fallback_rows:
        raise ExecutionError(
            code="native_route_ledger_invalid",
            message=(
                f"{table!r}: native route ledger shows a non-zero oracle/fallback count "
                "on a lane with no call site for either; refusing to commit."
            ),
        )
    if len(ledger.records) != ledger.native_completed:
        raise ExecutionError(
            code="native_route_ledger_invalid",
            message=(
                f"{table!r}: native route ledger holds {len(ledger.records)} record(s) but "
                f"{ledger.native_completed} completed call(s); refusing to commit."
            ),
        )
    identities = [(r.table, r.node, r.chunk_index) for r in ledger.records]
    if len(set(identities)) != len(identities):
        raise ExecutionError(
            code="native_route_ledger_invalid",
            message=(
                f"{table!r}: native route ledger records are not unique per "
                "(table, work-node, chunk index); refusing to commit."
            ),
        )
    foreign = [r for r in ledger.records if r.table != table]
    if foreign:
        raise ExecutionError(
            code="native_route_ledger_invalid",
            message=(
                f"{table!r}: native route ledger holds {len(foreign)} record(s) attributed "
                "to a different table; refusing to commit."
            ),
        )


def _masked_batches(
    first: pa.RecordBatch | None,
    rest: Iterator[pa.RecordBatch],
    *,
    table: str,
    column_order: tuple[str, ...],
    strategy_cfg: dict[str, tuple[str, dict[str, Any]]],
    out_schema: pa.Schema,
    ledger: NativeRouteLedger,
    timing_acc: dict[tuple[str, str], float],
    boundary_ms_box: list[float],
    mem_acc: dict[tuple[str, str], int] | None = None,
    expected_schema: pa.Schema | None = None,
    digest_state: ExecutionDigestState | None = None,
) -> Iterator[pa.RecordBatch]:
    # mem_acc/expected_schema default so old direct-call tests need no change;
    # expected_schema defaults to first's schema (slice 1); widened passes the
    # frozen preflight schema instead (first can be None there).
    if mem_acc is None:
        mem_acc = {}
    if expected_schema is not None:
        schema_to_check = expected_schema
    elif first is not None:
        schema_to_check = first.schema
    else:  # pragma: no cover - precondition
        raise AssertionError("no schema to validate batches against")
    for i, batch in enumerate(_rechain(first, rest)):
        drift = _schema_drift_reason(schema_to_check, batch.schema)
        if drift is not None:
            ledger.rejected_chunks += 1
            raise ExecutionError(
                code="native_chunk_schema_drift",
                message=f"{table!r} chunk {i}: schema drift vs the admitted schema ({drift})",
            )
        if digest_state is not None:
            digest_state.observe_batch(batch)
        yield _mask_one_batch(
            batch,
            i,
            table=table,
            column_order=column_order,
            strategy_cfg=strategy_cfg,
            out_schema=out_schema,
            ledger=ledger,
            timing_acc=timing_acc,
            mem_acc=mem_acc,
            boundary_ms_box=boundary_ms_box,
        )
    # Verified only after the whole second read has streamed through, so a
    # mismatch aborts rather than silently truncating emitted output.
    if digest_state is not None:
        digest_state.verify(table=table)


def _execution_envelope(*, streaming: bool) -> dict[str, Any]:
    """The `quality_metrics["execution"]` shape, honest for this lane: a
    `LazySource` input is never resident here (admission requires it), so
    `loaded_fully_in_memory` is False regardless of sink mode -- unlike the
    sequential/out-of-core telemetry helper, whose `loaded_fully_in_memory`
    reasoning assumes a `source_loader`-shaped caller and would misreport
    this route's genuinely-lazy input as resident."""
    return {
        "execution_mode": _STREAMING_EXECUTION_MODE,
        "route_reason": "native_route_admitted",
        "eviction": "per_batch",
        "outputs_streamed": streaming,
        "loaded_fully_in_memory": False,
    }


def _execution_adapter_stamp(*, resolved_substrate: str) -> dict[str, Any]:
    """The `quality_metrics["execution_adapter"]` reproducibility stamp for
    an admitted native run. The native lane always runs non-default (the
    caller had to opt in with `native_route_enabled=True`), so -- unlike
    `_pipeline_finalize.stamp_execution_metrics`'s pandas/polars stamp,
    which only fires when a knob differs from its default -- this one is
    unconditional whenever the lane actually admitted.

    Only fields with a real, measured value are included: the lane has no
    FPE/worker/fallback knobs, so `fpe_chunk_count` / `max_workers` /
    `fallback_to_pandas` would be fabricated placeholders here and are
    omitted rather than copied from the pandas/polars schema. `adapter_
    version` is pyarrow's, since the kernels are pyarrow-array functions
    (`decoy_engine.execution.native._kernels_scalar`), not pandas' or
    polars'.
    """
    return {
        "adapter_name": "native",
        "adapter_version": pa.__version__,
        "resolved_substrate": resolved_substrate,
    }


def _run_native_streaming(
    *,
    table: str,
    plan: Plan,
    config: Mapping[str, Any],
    column_order: tuple[str, ...],
    first_batch: pa.RecordBatch | None,
    rest_batches: Iterator[pa.RecordBatch],
    sink: TransactionalSink | None,
    streaming: bool,
    table_kinds: dict[str, str],
    resolved_substrate: str,
    explain_plan: bool,
    execution_plan_decision: ExecutionPlan | None,
    source_schema: pa.Schema | None = None,
    digest_state: ExecutionDigestState | None = None,
) -> tuple[ExecutionResult, NativeRouteReport]:
    del plan  # admission already resolved the declared-column set; unused here
    strategy_cfg = _resolve_strategy_cfg(config, table, column_order)
    if source_schema is None:
        if first_batch is None:  # pragma: no cover - precondition
            raise AssertionError("no schema: first_batch and source_schema both None")
        source_schema = first_batch.schema

    # Passthrough keeps the source type (widened admission covers int/bool/
    # timestamp, not just utf8); redact/truncate always emit a string.
    def _col_type(name: str) -> pa.DataType:
        if strategy_cfg[name][0] == "passthrough":
            return source_schema.field(name).type
        return pa.utf8()

    out_schema = pa.schema([pa.field(name, _col_type(name)) for name in column_order])
    ledger = NativeRouteLedger()
    timing_acc: dict[tuple[str, str], float] = {}
    mem_acc: dict[tuple[str, str], int] = {}
    boundary_ms_box = [0.0]

    def batches() -> Iterator[pa.RecordBatch]:
        return _masked_batches(
            first_batch,
            rest_batches,
            table=table,
            column_order=column_order,
            strategy_cfg=strategy_cfg,
            out_schema=out_schema,
            ledger=ledger,
            timing_acc=timing_acc,
            mem_acc=mem_acc,
            boundary_ms_box=boundary_ms_box,
            expected_schema=source_schema,
            digest_state=digest_state,
        )

    out_table: pa.Table | None = None
    if streaming:
        if (
            sink is None
        ):  # pragma: no cover - precondition: sink_mode="streaming" implies a real sink
            raise AssertionError(
                "streaming=True but no sink was supplied; caller precondition violated"
            )
        committed = False
        try:
            sink.write_batches(table, batches(), schema=out_schema)
            _validate_ledger(ledger, table=table)
            sink.commit()
            committed = True
        except BaseException:
            if not committed:
                try:
                    sink.abort()
                except Exception:
                    pass
            raise
    else:
        masked = list(batches())
        _validate_ledger(ledger, table=table)
        out_table = pa.Table.from_batches(masked, schema=out_schema)

    timings = tuple(
        StrategyTimingRecord(
            strategy_type=strategy,
            column=name,
            elapsed_ms=ms,
            peak_memory_delta_kb=mem_acc.get((strategy, name), 0),
        )
        for (strategy, name), ms in timing_acc.items()
    )
    report = NativeRouteReport(
        attempted=True, admitted=True, table=table, reason=None, ledger=ledger
    )
    outputs: dict[str, pa.Table] = {} if out_table is None else {table: out_table}
    # Route the native result through the same two reproducibility stamps
    # the chunked/full_frame continuation gets (`_pipeline_finalize.
    # stamp_execution_metrics` + `_pipeline.py`'s `explain_plan` block), so
    # `explain_plan=True` on a native-admitted job is not silently blind --
    # the values here are the lane's real, already-computed parameters
    # (never fabricated): `resolved_substrate` is what admission itself
    # required to be `"pandas"`, and `execution_plan_decision` is the SAME
    # classification `run_pipeline` computed once, before this call, for
    # every route (see `_pipeline.py`'s `decide_chunk_route` call site).
    quality_metrics: dict[str, Any] = {
        "execution": _execution_envelope(streaming=streaming),
        "execution_adapter": _execution_adapter_stamp(resolved_substrate=resolved_substrate),
    }
    if explain_plan and execution_plan_decision is not None:
        quality_metrics["execution_plan"] = {
            "mode": execution_plan_decision.mode,
            "reason": execution_plan_decision.reason,
            "rejections": dict(execution_plan_decision.rejections),
        }
    result = ExecutionResult(
        outputs=outputs,
        timings=timings,
        boundary_conversion_ms=boundary_ms_box[0],
        warnings=(),  # oracle-equivalent: none of the three strategies warn
        quality_metrics=quality_metrics,
        table_kinds=table_kinds,
        row_errors=(),
        native_route=report,
    )
    return result, report


def try_native_route(
    *,
    config: dict[str, Any],
    plan: Plan,
    table_kinds: dict[str, str],
    caller_sources: Mapping[str, pa.Table | LazySource],
    source_loader: Callable[[str], pa.Table] | None,
    sink: TransactionalSink | None,
    fidelity_report: bool,
    execution_mode: str,
    graph: RelationshipGraph,
    resolved_substrate: str = "pandas",
    explain_plan: bool = False,
    execution_plan_decision: ExecutionPlan | None = None,
    batch_rows: int = _NATIVE_BATCH_ROWS_DEFAULT,
) -> tuple[ExecutionResult | None, NativeRouteReport]:
    """Try the native lane for this invocation; called only when the caller
    has already opted in (`native_route_enabled=True`).

    Returns `(None, report)` for every reroute (no source was touched for a
    static decline; exactly one batch was read for a dynamic decline, then
    discarded -- the caller's `caller_sources` mapping is untouched, so the
    ordinary continuation re-opens the same `LazySource` fresh). Returns
    `(ExecutionResult, report)` once the whole pass committed. Raises past
    that point; see the module docstring.

    `resolved_substrate` (default `"pandas"`) is threaded straight to
    `static_candidacy`'s own substrate gate. `explain_plan` / `execution_
    plan_decision` only affect committed telemetry, never admission.
    """
    candidacy = static_candidacy(
        config=config,
        execution_mode=execution_mode,
        table_kinds=table_kinds,
        caller_sources=caller_sources,
        source_loader=source_loader,
        sink=sink,
        fidelity_report=fidelity_report,
        graph=graph,
        resolved_substrate=resolved_substrate,
    )
    if not candidacy.candidate:
        return None, NativeRouteReport(
            attempted=False, admitted=False, table=None, reason=candidacy.reason, ledger=None
        )

    table = candidacy.table
    if table is None:  # pragma: no cover - precondition: candidate=True always sets table
        raise AssertionError("static_candidacy admitted a candidate with no table name")
    source = caller_sources[table]
    if not isinstance(
        source, LazySource
    ):  # pragma: no cover - static_candidacy already proved this
        raise AssertionError(f"{table!r}: candidacy admitted a non-LazySource entry")

    # Footer schema alone decides utf8-only (slice 1's path) vs widened.
    classification = classify_and_preflight(
        source, table=table, plan=plan, config=config, batch_rows=batch_rows
    )
    if classification.mode == "utf8_only":
        admission = peek_and_admit(source, table=table, plan=plan, batch_rows=batch_rows)
        if not admission.admitted:
            return None, NativeRouteReport(
                attempted=True, admitted=False, table=table, reason=admission.reason, ledger=None
            )
        if admission.first_batch is None or admission.rest is None:  # pragma: no cover
            raise AssertionError(f"{table!r}: admitted with no batch/iterator")
        return _run_native_streaming(
            table=table,
            plan=plan,
            config=config,
            column_order=admission.column_order,
            first_batch=admission.first_batch,
            rest_batches=admission.rest,
            sink=sink,
            streaming=candidacy.sink_mode == "streaming",
            table_kinds=table_kinds,
            resolved_substrate=resolved_substrate,
            explain_plan=explain_plan,
            execution_plan_decision=execution_plan_decision,
        )

    if not classification.admitted:
        return None, NativeRouteReport(
            attempted=True, admitted=False, table=table, reason=classification.reason, ledger=None
        )
    return run_widened_execution(
        classification,
        source=source,
        table=table,
        plan=plan,
        config=config,
        table_kinds=table_kinds,
        sink=sink,
        streaming=candidacy.sink_mode == "streaming",
        resolved_substrate=resolved_substrate,
        explain_plan=explain_plan,
        execution_plan_decision=execution_plan_decision,
        batch_rows=batch_rows,
    )


__all__ = ["try_native_route"]
