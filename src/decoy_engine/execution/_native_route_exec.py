"""Production entry point for the single-pass streaming native lane.

`try_native_route` is what `run_pipeline` (`_pipeline.py`) calls, right after
layer-1 FK routing and before `resolve_resident_sources` -- see
`_native_route.py`'s module docstring for why that placement is load-bearing.
It returns `(ExecutionResult, report)` when the whole single pass masked and
committed, `(None, report)` when the job reroutes to the ordinary oracle
continuation (the caller falls through unchanged; the original `LazySource`
was never exhausted, so `resolve_resident_sources` reads it fresh), and
raises once the pass is actually underway -- 2.6's "no oracle fallback after
the first native output" is not a special case here, it is the only
behavior: everything past `peek_and_admit` either fully commits or raises.
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
from decoy_engine.execution.native._kernels_scalar import (
    native_passthrough,
    native_redact,
    native_truncate,
)
from decoy_engine.instrumentation.timing import StrategyTimingRecord
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
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


def _rechain(first: pa.RecordBatch, rest: Iterator[pa.RecordBatch]) -> Iterator[pa.RecordBatch]:
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
    boundary_ms_box: list[float],
) -> pa.RecordBatch:
    """Mask one batch column-by-column, updating the ledger and per-column
    timing as each kernel call actually completes -- never in advance."""
    t_batch0 = time.perf_counter()
    arrays: list[pa.Array] = []
    col_time_total = 0.0
    n = batch.num_rows
    for name in column_order:
        strategy, kwargs = strategy_cfg[name]
        source = batch.column(name)
        ledger.native_attempted += 1
        ledger.native_rows_attempted += n
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
        col_time_total += elapsed
        key = (strategy, name)
        timing_acc[key] = timing_acc.get(key, 0.0) + elapsed * 1000.0
        arrays.append(arr)
        ledger.native_completed += 1
        ledger.native_rows_completed += n
        ledger.records.append(LedgerEntry(table=table, node=name, chunk_index=chunk_index))
    batch_total = time.perf_counter() - t_batch0
    # Assembly/schema-binding time, not attributed to any one column's kernel.
    boundary_ms_box[0] += max(0.0, batch_total - col_time_total) * 1000.0
    return pa.RecordBatch.from_arrays(arrays, schema=out_schema)


def _validate_ledger(ledger: NativeRouteLedger, *, table: str) -> None:
    """Fail closed before any commit if the ledger does not prove a clean
    single pass: an attempted call with no matching completion (a partial
    kernel failure the caller somehow swallowed), or a stray oracle/fallback
    call (there is no code path to one on this lane, so a non-zero count
    here means something else corrupted the ledger object)."""
    if ledger.native_attempted != ledger.native_completed:
        raise ExecutionError(
            code="native_route_ledger_invalid",
            message=(
                f"{table!r}: native route ledger shows {ledger.native_attempted} attempted "
                f"vs {ledger.native_completed} completed calls; refusing to commit."
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


def _masked_batches(
    first: pa.RecordBatch,
    rest: Iterator[pa.RecordBatch],
    *,
    table: str,
    column_order: tuple[str, ...],
    strategy_cfg: dict[str, tuple[str, dict[str, Any]]],
    out_schema: pa.Schema,
    ledger: NativeRouteLedger,
    timing_acc: dict[tuple[str, str], float],
    boundary_ms_box: list[float],
) -> Iterator[pa.RecordBatch]:
    expected_schema = first.schema
    for i, batch in enumerate(_rechain(first, rest)):
        if i > 0:
            drift = _schema_drift_reason(expected_schema, batch.schema)
            if drift is not None:
                ledger.rejected_chunks += 1
                raise ExecutionError(
                    code="native_chunk_schema_drift",
                    message=f"{table!r} chunk {i}: schema drift vs the admitted first batch ({drift})",
                )
        yield _mask_one_batch(
            batch,
            i,
            table=table,
            column_order=column_order,
            strategy_cfg=strategy_cfg,
            out_schema=out_schema,
            ledger=ledger,
            timing_acc=timing_acc,
            boundary_ms_box=boundary_ms_box,
        )


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


def _run_native_streaming(
    *,
    table: str,
    plan: Plan,
    config: Mapping[str, Any],
    column_order: tuple[str, ...],
    first_batch: pa.RecordBatch,
    rest_batches: Iterator[pa.RecordBatch],
    sink: TransactionalSink | None,
    streaming: bool,
    table_kinds: dict[str, str],
) -> tuple[ExecutionResult, NativeRouteReport]:
    del plan  # admission already resolved the declared-column set; unused here
    strategy_cfg = _resolve_strategy_cfg(config, table, column_order)
    out_schema = pa.schema([pa.field(name, pa.utf8()) for name in column_order])
    ledger = NativeRouteLedger()
    timing_acc: dict[tuple[str, str], float] = {}
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
            boundary_ms_box=boundary_ms_box,
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
            strategy_type=strategy, column=name, elapsed_ms=ms, peak_memory_delta_kb=0
        )
        for (strategy, name), ms in timing_acc.items()
    )
    report = NativeRouteReport(
        attempted=True, admitted=True, table=table, reason=None, ledger=ledger
    )
    outputs: dict[str, pa.Table] = {} if out_table is None else {table: out_table}
    result = ExecutionResult(
        outputs=outputs,
        timings=timings,
        boundary_conversion_ms=boundary_ms_box[0],
        warnings=(),  # oracle-equivalent: none of the three strategies warn
        quality_metrics={"execution": _execution_envelope(streaming=streaming)},
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

    admission = peek_and_admit(source, table=table, plan=plan, batch_rows=batch_rows)
    if not admission.admitted:
        return None, NativeRouteReport(
            attempted=True, admitted=False, table=table, reason=admission.reason, ledger=None
        )

    if admission.first_batch is None or admission.rest is None:  # pragma: no cover - precondition
        raise AssertionError(f"{table!r}: admission reported admitted=True with no batch/iterator")
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
    )


__all__ = ["try_native_route"]
