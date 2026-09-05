"""Admission for the production single-pass streaming native lane (Q3 slice 1).

`decide_chunk_route` (`_pipeline_chunk_route.py`) and this module's
`static_candidacy` both run at the same call site in `_pipeline.py`, on the
same config/plan/schema-only inputs -- neither touches a source. That split
matters: layer-1 FK routing and an explicit `sequential`/`full_frame`/
`out_of_core` `execution_mode` must never cause a `LazySource` to be opened
just to evaluate native candidacy, so every check that can be answered from
`config`, the compiled `Plan`, and the routing signals already in hand runs
here, BEFORE anything reads a batch. Only after `static_candidacy` returns a
candidate does `_native_route_exec.peek_and_admit` open the source, and it
opens it exactly once (`LazySource.iter_batches`), taking its first batch as
both the schema-admission evidence and the first chunk of the single
masking pass -- never a separate probe read.

Scope (docs/plans/2026-09-04-native-route-production-seam.md 2.1-2.5): the
three pure-kernel strategies (`passthrough`, `redact`, `truncate`) over an
EXACT `pa.utf8()` column, on a single non-FK mask table whose source is a
`LazySource` with no `source_loader` in play, only under
`execution_mode="auto"`, and only when the caller's resolved substrate is
`"pandas"` (the kernels are proven byte-identical to the pandas oracle, not
polars). Every other shape -- FK, `vault: true`, a validator, a fidelity
request, quarantine, a non-streaming sink, multi-table, a generate table, a
non-`LazySource`/loader-driven source, an off-allowlist strategy, a
non-pandas substrate, or a redact/truncate config the shared
`_requirements.py` gates would reject -- reroutes to the oracle with a coded
reason, closed-world.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa

from decoy_engine.execution._output_projection import known_output_columns
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution.native._requirements import (
    redact_config_rejection,
    truncate_config_rejection,
)
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships import RelationshipGraph

# The closed narrow allowlist for this slice -- NOT `native.NATIVE_KERNEL_
# STRATEGIES` (which also carries `hash`, out of scope here: no keyed-mask
# secret handling or ABI probe belongs on this lane yet).
ALLOWED_STRATEGIES = frozenset({"passthrough", "redact", "truncate"})

SinkMode = Literal["resident", "streaming"]


@dataclass(frozen=True)
class NativeStaticCandidacy:
    """The no-I/O verdict: whether `table` is even worth peeking a batch for.

    `sink_mode` is set only when `candidate` is True: `"resident"` for
    `sink is None` (the caller wants the whole masked table back, no
    flat-RSS claim), `"streaming"` for `type(sink) is ParquetTransactionalSink`
    exactly (a retaining subclass is NOT this mode -- literal type check,
    not `isinstance`, matching 2.4's structural-sink caveat).
    """

    candidate: bool
    table: str | None
    reason: str | None
    sink_mode: SinkMode | None


@dataclass(frozen=True)
class NativeBatchAdmission:
    """The one-batch-read verdict, decided from the FIRST execution batch.

    `first_batch` / `rest` are populated only when `admitted` is True; the
    caller masks `first_batch` and every batch `rest` yields as the single
    pass (see `_rechain` in `_native_route_exec.py`). A caller must not touch
    `rest` when `admitted` is False -- the iterator is left unconsumed
    (harmless: a `LazySource` batch iterator holds no resource beyond a
    generator frame, and the caller re-opens the source fresh via the
    ordinary `resolve_resident_sources` path).
    """

    admitted: bool
    reason: str | None
    column_order: tuple[str, ...]
    first_batch: pa.RecordBatch | None
    rest: Iterator[pa.RecordBatch] | None


def _find_table(config: Mapping[str, Any], table: str) -> dict[str, Any] | None:
    for tbl in config.get("tables") or ():
        if isinstance(tbl, dict) and tbl.get("name") == table:
            return tbl
    return None


def static_candidacy(
    *,
    config: Mapping[str, Any],
    execution_mode: str,
    table_kinds: Mapping[str, str],
    caller_sources: Mapping[str, pa.Table | LazySource],
    source_loader: Callable[[str], pa.Table] | None,
    sink: TransactionalSink | None,
    fidelity_report: bool,
    graph: RelationshipGraph,
    resolved_substrate: str = "pandas",
) -> NativeStaticCandidacy:
    """The config/plan/schema-only admission gate -- no I/O, no source touch.

    Callers must check `execution_mode == "auto"` reaches this function only
    for jobs that took neither the sequential nor the out-of-core layer-1
    early return (`run_pipeline`'s call site enforces this by placement, not
    by a check here); the `execution_mode` guard below is the second,
    independent gate an explicit override cannot bypass regardless of call
    order.

    `resolved_substrate` gates the same way: the native kernels are proven
    byte-identical to the PANDAS oracle only (`_kernels_scalar.py`'s parity
    contract), so an explicit `substrate="polars"` (or an env-resolved one)
    must fall through to the caller's requested substrate rather than have
    native silently override it. Defaults to `"pandas"` so every existing
    direct caller of this function (unit tests that predate the substrate
    thread-through) keeps admitting exactly as before.
    """

    def _decline(reason: str) -> NativeStaticCandidacy:
        return NativeStaticCandidacy(candidate=False, table=None, reason=reason, sink_mode=None)

    if execution_mode != "auto":
        return _decline("execution_mode_not_auto")

    if resolved_substrate != "pandas":
        return _decline(f"non_pandas_substrate:{resolved_substrate}")

    mask_tables = [name for name, kind in table_kinds.items() if kind == "mask"]
    if any(kind == "generate" for kind in table_kinds.values()):
        return _decline("generation_table_present")
    if len(mask_tables) != 1:
        return _decline("multi_table_job")
    table = mask_tables[0]

    if graph.edges:
        # Layer-1 (`decide_execution_route`) already routes every relationship-
        # bearing pure-mask job to sequential or out-of-core; a relationship
        # job disqualified from BOTH (a cyclic FK graph, or one disqualified
        # from sequential for a reason unrelated to its FK shape) still lands
        # on full_frame, which is exactly where this call site sits. Reasserted
        # here, before any source is touched, so an FK job can never reach the
        # per-batch peek regardless of why it fell through to full_frame.
        return _decline("fk_relationship_present")

    if source_loader is not None:
        return _decline("source_loader_present")

    source = caller_sources.get(table)
    if not isinstance(source, LazySource):
        return _decline("non_lazy_source")

    if fidelity_report:
        return _decline("fidelity_report_requested")
    if config.get("validators"):
        return _decline("validators_present")
    if config.get("quarantine"):
        return _decline("quarantine_configured")

    if sink is None:
        sink_mode: SinkMode = "resident"
    elif type(sink) is ParquetTransactionalSink:
        sink_mode = "streaming"
    else:
        return _decline("unsupported_sink")

    table_cfg = _find_table(config, table)
    columns = (table_cfg or {}).get("columns") or ()
    if not columns:
        return _decline("no_columns_configured")
    for col in columns:
        if not isinstance(col, dict):
            return _decline("invalid_column_config")
        name = col.get("name", "?")
        if bool(col.get("vault", False)):
            return _decline(f"vault_column:{name}")
        strategy = col.get("strategy")
        if strategy not in ALLOWED_STRATEGIES:
            return _decline(f"unsupported_strategy:{name}:{strategy}")
        provider_config = col.get("provider_config")
        cfg = provider_config if isinstance(provider_config, dict) else {}
        if strategy == "redact":
            reason = redact_config_rejection(name, cfg)
            if reason is not None:
                return _decline(reason)
        elif strategy == "truncate":
            reason = truncate_config_rejection(name, cfg)
            if reason is not None:
                return _decline(reason)

    return NativeStaticCandidacy(candidate=True, table=table, reason=None, sink_mode=sink_mode)


def peek_and_admit(
    source: LazySource, *, table: str, plan: Plan, batch_rows: int
) -> NativeBatchAdmission:
    """Open `source` exactly once and decide dynamic admission from its FIRST
    batch: a zero-row source reroutes (2.2 -- the oracle types `redact`/
    `truncate` output `double` there, `native` would emit `string`), an
    unsupported projection reroutes (the batch's columns must equal the
    plan's declared columns for `table` exactly, no unconfigured passthrough
    column this lane has no policy wiring for), and every declared column
    must be exact `pa.utf8()` -- `large_utf8`, any `dictionary<*, utf8>`, and
    every other Arrow type reroute.
    """
    batches = source.iter_batches(batch_rows)
    first = next(batches, None)
    if first is None:
        return NativeBatchAdmission(
            admitted=False, reason="zero_row_source", column_order=(), first_batch=None, rest=None
        )

    declared = known_output_columns(plan, table)
    actual = set(first.schema.names)
    if actual != declared:
        missing = sorted(declared - actual)
        extra = sorted(actual - declared)
        return NativeBatchAdmission(
            admitted=False,
            reason=f"unsupported_projection:missing={missing}:extra={extra}",
            column_order=(),
            first_batch=None,
            rest=None,
        )

    column_order = tuple(first.schema.names)
    for name in column_order:
        ftype = first.schema.field(name).type
        if ftype != pa.utf8():
            return NativeBatchAdmission(
                admitted=False,
                reason=f"non_utf8_column:{name}:{ftype!s}",
                column_order=(),
                first_batch=None,
                rest=None,
            )
    return NativeBatchAdmission(
        admitted=True, reason=None, column_order=column_order, first_batch=first, rest=batches
    )


@dataclass(frozen=True)
class LedgerEntry:
    """One (table, work-node, chunk index) identity actually masked natively."""

    table: str
    node: str
    chunk_index: int


@dataclass
class NativeRouteLedger:
    """Invocation-scoped counters, incremented at the ACTUAL call boundaries
    (2.8): never derived from a planned route table. `attempted` /
    `completed` move together for every synchronous kernel call that
    returns normally; a mid-stream failure leaves `attempted > completed`,
    which `_native_route_exec._validate_ledger` reads as proof a commit must
    not happen. `oracle_*` / `fallback_*` have no call site on this lane's
    admitted path (there is no code path from an admitted decision back to
    the oracle), so they read zero by construction -- restated as fields
    (not just prose) so a caller can assert the frozen Part-1 zero-count
    contract directly off this object.
    """

    native_attempted: int = 0
    native_completed: int = 0
    native_rows_attempted: int = 0
    native_rows_completed: int = 0
    oracle_calls: int = 0
    oracle_rows: int = 0
    fallback_calls: int = 0
    fallback_rows: int = 0
    rejected_chunks: int = 0
    records: list[LedgerEntry] = field(default_factory=list)


@dataclass(frozen=True)
class NativeRouteReport:
    """Job evidence for one `run_pipeline` invocation's native-route decision.

    `attempted` is True once `static_candidacy` admits (a batch was peeked,
    whether or not it was ultimately admitted); `admitted` is True only once
    the whole single pass masked, was validated, and committed. `ledger` is
    populated only when `attempted` is True.
    """

    attempted: bool
    admitted: bool
    table: str | None
    reason: str | None
    ledger: NativeRouteLedger | None


__all__ = [
    "ALLOWED_STRATEGIES",
    "LedgerEntry",
    "NativeBatchAdmission",
    "NativeRouteLedger",
    "NativeRouteReport",
    "NativeStaticCandidacy",
    "SinkMode",
    "peek_and_admit",
    "static_candidacy",
]
