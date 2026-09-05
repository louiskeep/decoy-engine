"""Bounded preflight, admission matrix, and source-snapshot digest for the
native route's widened admission (Q3 slice 2).

Slice 1 admitted `utf8` columns on schema alone: the oracle's `pa.Table.
from_pandas` round-trip infers the same type for a `utf8` column regardless
of null pattern, so a first-batch peek was enough evidence. Integer, boolean,
and timestamp lack that property (a null-bearing integer widens to `float64`
in pandas; an all-null or empty column infers a different Arrow type than a
value-bearing one), so admitting them needs each column's GLOBAL null/empty
state before committing to native output -- a first-batch peek cannot see a
null landing in row 50,001.

`classify_and_preflight` is the single entry point `_native_route_exec.py`
calls: it reads `LazySource.schema` (the footer, no batch I/O) to decide
whether every declared column is `utf8` (the slice-1 fast path, still routed
through `peek_and_admit` unchanged) or whether at least one column needs the
bounded preflight scan (`run_preflight`, one full streaming pass, the FIRST
of the widened lane's two runtime reads). `ExecutionDigestState` is the
matching second-pass accumulator: execution recomputes the identical digest
streaming the second read, compared to the frozen preflight digest after
that iterator fully drains and before commit -- see `run_preflight`'s and
`ExecutionDigestState.verify`'s docstrings for why a footer/row-count check
alone is not enough (`LazySource` reopens the file every read; nothing else
pins the two reads together).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._native_route_digest import (
    ColumnState,
    PreflightColumnAccumulator,
    combine_column_digests,
    resolve_column_state,
)
from decoy_engine.execution._output_projection import known_output_columns

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.execution._native_route import NativeRouteReport
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._readers import LazySource

# 1. The four-state resolver (`ColumnState`, `resolve_column_state`) lives in
# `_native_route_digest` alongside the accumulator that reports through it;
# both are re-exported below so callers keep importing them from here.

# 2. Type-family resolution and the normative admission matrix

TypeFamily = Literal["utf8", "integer", "boolean", "timestamp"]


def type_family(arrow_type: pa.DataType) -> TypeFamily | None:
    """The matrix family for `arrow_type`, or None for a type this lane never
    admits (`large_utf8`, `decimal128`, `float64`, dictionary-encoded, ...).

    `utf8` is its own family so a caller can route it straight to the
    schema-only slice-1 path (it has no matrix row: admission there never
    depends on null state, unchanged by this slice).
    """
    if arrow_type == pa.utf8():
        return "utf8"
    if pa.types.is_boolean(arrow_type):
        return "boolean"
    if pa.types.is_integer(arrow_type):
        return "integer"
    if pa.types.is_timestamp(arrow_type):
        return "timestamp"
    return None


# Normative table, docs/plans/2026-09-05-native-route-wider-types.md section 3.
# True = Admit, False = Reroute-to-oracle. "Oracle rejects" cells (truncate /
# integer / partial-null and all-null) collapse to False here: the action is
# identical (the whole table goes to the ordinary continuation before output),
# and the existing execution-time guard (`_guards.reject_null_bearing_int`)
# raises its own coded error once that continuation actually runs -- this
# resolver has no separate "reject" outcome to express, see plan section 6.
def _row(
    no_null: bool, partial_null: bool, all_null: bool, empty: bool
) -> dict[ColumnState, bool]:  # pragma: no mutate block
    # Excluded from mutation, not for coverage reasons but because it is called
    # at MODULE-IMPORT time to build `_ADMISSION_MATRIX`; a trampolined mutant
    # here raises during pytest COLLECTION under mutmut's forced-fail probe
    # (rc 2), which the tq_mutate soundness gate reads as a broken harness. The
    # matrix cells it produces are pinned exactly by
    # `test_admission_matrix_matches_normative_table` (every cell asserted
    # against the normative table), so excluding this helper hides no gap -- and
    # since every mutant it would generate is already killed by that test,
    # excluding it can only lower the raw count, never inflate the score.
    return {"no_null": no_null, "partial_null": partial_null, "all_null": all_null, "empty": empty}


# fmt: off
_ADMISSION_MATRIX: dict[tuple[str, TypeFamily], dict[ColumnState, bool]] = {
    ("passthrough", "integer"):   _row(True,  False, False, True),
    ("passthrough", "boolean"):   _row(True,  True,  False, True),
    ("passthrough", "timestamp"): _row(True,  True,  True,  True),
    ("redact", "integer"):        _row(True,  True,  False, False),
    ("redact", "boolean"):        _row(True,  True,  False, False),
    ("redact", "timestamp"):      _row(True,  True,  False, False),
    ("truncate", "integer"):      _row(True,  False, False, False),
    ("truncate", "boolean"):      _row(True,  True,  False, False),
    ("truncate", "timestamp"):    _row(True,  True,  False, False),
}
# fmt: on


def resolve_admission(strategy: str, family: TypeFamily, state: ColumnState) -> bool:
    """The matrix verdict for one column, True meaning Admit. `family="utf8"`
    has no row (utf8 admission stays schema-only, per slice 1); asking for
    one is a caller bug, not a data condition, hence the plain KeyError.
    """
    return _ADMISSION_MATRIX[(strategy, family)][state]


# 3. The source-snapshot digest codec (`PreflightColumnAccumulator`, the
# hasher framing, the per-array/per-column/whole-source functions) lives in
# `_native_route_digest`; `combine_column_digests` is re-exported below.

# 4. Schema-drift guard shared by the preflight pass and the execution pass


def schema_drift_reason(baseline: pa.Schema, actual: pa.Schema) -> str | None:
    """None when `actual` still matches `baseline` in column set, order, and
    per-column type; otherwise a coded description of the drift.

    `baseline.names` order IS checked (`!=` on the tuples), not just set
    equality: a reordered schema is drift too, since the frozen column order
    is part of the digest's identity (section 4).
    """
    if tuple(actual.names) != tuple(baseline.names):
        missing = sorted(set(baseline.names) - set(actual.names))
        extra = sorted(set(actual.names) - set(baseline.names))
        return f"columns_changed:missing={missing}:extra={extra}"
    for name in baseline.names:
        want, got = baseline.field(name).type, actual.field(name).type
        if want != got:
            return f"type_changed:{name}:{want}->{got}"
    return None


# 5. The bounded preflight pass


@dataclass(frozen=True)
class PreflightResult:
    admitted: bool
    reason: str | None
    schema: pa.Schema
    digest: bytes | None
    column_states: Mapping[str, ColumnState]


def run_preflight(
    source: LazySource,
    *,
    baseline_schema: pa.Schema,
    column_order: tuple[str, ...],
    strategy_by_column: Mapping[str, str],
    batch_rows: int,
) -> PreflightResult:
    """The first of the two runtime reads (plan section 2): stream every
    batch against `baseline_schema` (the footer schema, available even for
    a zero-batch empty file), accumulating per-column counts and the
    digest. Resolves the matrix verdict only once the whole source has been
    seen, so a null at row 50,001 rejects exactly like one at row 5.
    """
    accumulators = {
        name: PreflightColumnAccumulator(name=name, arrow_type=baseline_schema.field(name).type)
        for name in column_order
    }
    for batch in source.iter_batches(batch_rows):
        drift = schema_drift_reason(baseline_schema, batch.schema)
        if drift is not None:
            return PreflightResult(
                admitted=False,
                reason=f"native_preflight_schema_drift:{drift}",
                schema=baseline_schema,
                digest=None,
                column_states={},
            )
        for name in column_order:
            accumulators[name].observe(batch.column(name))

    column_states: dict[str, ColumnState] = {}
    for name in column_order:
        acc = accumulators[name]
        state = acc.state()
        column_states[name] = state
        family = type_family(acc.arrow_type)
        if family is None or family == "utf8":
            # utf8 has no matrix row (unconditional admit, unchanged from
            # slice 1); `family is None` cannot occur here -- the caller
            # only reaches `run_preflight` after every declared column's
            # family already resolved to something admittable.
            continue
        strategy = strategy_by_column[name]
        if not resolve_admission(strategy, family, state):
            return PreflightResult(
                admitted=False,
                reason=f"native_preflight_reroute:{name}:{strategy}:{family}:{state}",
                schema=baseline_schema,
                digest=None,
                column_states=column_states,
            )

    digest = combine_column_digests([accumulators[name].digest() for name in column_order])
    return PreflightResult(
        admitted=True,
        reason=None,
        schema=baseline_schema,
        digest=digest,
        column_states=column_states,
    )


# 6. Execution-side digest recomputation


@dataclass
class ExecutionDigestState:
    """The second pass's accumulator: rebuilt fresh for execution (never
    reuses the preflight instance -- proving the SECOND read agrees, not
    trusting the first)."""

    column_order: tuple[str, ...]
    schema: pa.Schema
    expected_digest: bytes
    _accumulators: dict[str, PreflightColumnAccumulator] = field(init=False)

    def __post_init__(self) -> None:
        self._accumulators = {
            name: PreflightColumnAccumulator(name=name, arrow_type=self.schema.field(name).type)
            for name in self.column_order
        }

    def observe_batch(self, batch: pa.RecordBatch) -> None:
        for name in self.column_order:
            self._accumulators[name].observe(batch.column(name))

    def verify(self, *, table: str) -> None:
        """Raise iff the fully-drained execution read digests differently
        than the preflight read did. Called only after the LAST batch has
        been yielded (see `_native_route_exec._masked_batches`), so this
        always runs before `sink.commit()` / the resident return -- never a
        retry, never an oracle fallback (plan section 4/5)."""
        actual = combine_column_digests(
            [self._accumulators[name].digest() for name in self.column_order]
        )
        if actual != self.expected_digest:
            raise ExecutionError(
                code="native_source_snapshot_digest_mismatch",
                message=(
                    f"{table!r}: the source's content changed between the native "
                    "preflight read and the execution read; refusing to commit."
                ),
            )


# 7. classify_and_preflight: the single entry point _native_route_exec calls


@dataclass(frozen=True)
class RouteAdmission:
    """`mode="utf8_only"`: every column is `utf8`; the caller runs the
    UNCHANGED slice-1 `peek_and_admit` path. `mode="widened"`: `admitted`
    distinguishes a resolved verdict (`schema`/`digest`/`column_order` set)
    from a decline (`reason` set)."""

    mode: Literal["utf8_only", "widened"]
    admitted: bool = False
    reason: str | None = None
    schema: pa.Schema | None = None
    digest: bytes | None = None
    column_order: tuple[str, ...] = ()


def _find_table(config: Mapping[str, Any], table: str) -> dict[str, Any] | None:
    for tbl in config.get("tables") or ():
        if isinstance(tbl, dict) and tbl.get("name") == table:
            return tbl
    return None


def _strategy_by_column(
    config: Mapping[str, Any], table: str, column_order: tuple[str, ...]
) -> dict[str, str]:
    table_cfg = _find_table(config, table)
    by_name = {
        col.get("name"): col
        for col in (table_cfg or {}).get("columns") or ()
        if isinstance(col, dict)
    }
    resolved: dict[str, str] = {}
    for name in column_order:
        col = by_name.get(name)
        strategy = col.get("strategy") if isinstance(col, dict) else None
        if not isinstance(strategy, str):  # pragma: no cover - candidacy guarantees presence
            # Static candidacy already proved every declared column has a
            # strategy in this config, so this cannot fire in production; a
            # guarded coded error keeps an impossible config shape from
            # surfacing as a bare KeyError mid-preflight, matching how the
            # rest of the lane declines on an unexpected shape.
            raise ExecutionError(
                code="native_preflight_strategy_unresolved",
                message=f"{table!r}: column {name!r} has no resolvable strategy in the config.",
            )
        resolved[name] = strategy
    return resolved


def classify_and_preflight(
    source: LazySource,
    *,
    table: str,
    plan: Plan,
    config: Mapping[str, Any],
    batch_rows: int,
) -> RouteAdmission:
    """Decide utf8-only vs widened from the FOOTER schema (no batch I/O),
    then run the bounded preflight only when a declared column needs it. A
    schema/projection mismatch reroutes here, before any batch is read,
    matching `peek_and_admit`'s own projection/type reason strings.
    """
    schema = source.schema
    declared = known_output_columns(plan, table)
    actual = set(schema.names)
    if actual != declared:
        missing = sorted(declared - actual)
        extra = sorted(actual - declared)
        return RouteAdmission(
            mode="widened",
            admitted=False,
            reason=f"unsupported_projection:missing={missing}:extra={extra}",
        )

    column_order = tuple(schema.names)
    families: dict[str, TypeFamily] = {}
    for name in column_order:
        ftype = schema.field(name).type
        family = type_family(ftype)
        if family is None:
            return RouteAdmission(
                mode="widened", admitted=False, reason=f"non_utf8_column:{name}:{ftype!s}"
            )
        families[name] = family

    if all(family == "utf8" for family in families.values()):
        return RouteAdmission(mode="utf8_only")

    strategy_by_column = _strategy_by_column(config, table, column_order)
    preflight = run_preflight(
        source,
        baseline_schema=schema,
        column_order=column_order,
        strategy_by_column=strategy_by_column,
        batch_rows=batch_rows,
    )
    if not preflight.admitted:
        return RouteAdmission(mode="widened", admitted=False, reason=preflight.reason)
    return RouteAdmission(
        mode="widened",
        admitted=True,
        schema=preflight.schema,
        digest=preflight.digest,
        column_order=column_order,
    )


def run_widened_execution(
    classification: RouteAdmission,
    *,
    source: LazySource,
    table: str,
    plan: Plan,
    config: Mapping[str, Any],
    table_kinds: dict[str, str],
    sink: TransactionalSink | None,
    streaming: bool,
    resolved_substrate: str,
    explain_plan: bool,
    execution_plan_decision: ExecutionPlan | None,
    batch_rows: int,
) -> tuple[ExecutionResult, NativeRouteReport]:
    """The second (execution) read for a widened-admitted table: a fresh
    `iter_batches` call, wired through a fresh `ExecutionDigestState` into
    `_run_native_streaming` (imported locally -- `_native_route_exec` imports
    THIS module at top level, so the reverse import needs a function body,
    matching `_native_route.maybe_run_native_route`'s same cycle-break).
    `classification.admitted` must already be True; the caller's job.
    """
    from decoy_engine.execution._native_route_exec import _run_native_streaming

    if classification.schema is None or classification.digest is None:  # pragma: no cover
        raise AssertionError(f"{table!r}: widened admission missing its frozen schema/digest")
    second_read_schema, execution_batches = source.open_batches(batch_rows)
    # The second read must present the schema the preflight froze, checked here
    # from the same handle its batches come from. A zero-row second read yields
    # no batches, so the per-batch guard in _masked_batches never runs and the
    # digest observes no arrays: with nothing streamed, a reorder, rename, drop,
    # add, or same-name type swap between the two reads all go unseen and the
    # stale frozen-schema output commits. Fail closed rather than reroute: the
    # source broke the between-reads stability the route assumes, and the
    # oracle's third read could see a third state.
    drift = schema_drift_reason(classification.schema, second_read_schema)
    if drift is not None:
        raise ExecutionError(
            code="native_chunk_schema_drift",
            message=f"{table!r}: second-read schema drift vs the admitted schema ({drift})",
        )
    first = next(execution_batches, None)
    digest_state = ExecutionDigestState(
        column_order=classification.column_order,
        schema=classification.schema,
        expected_digest=classification.digest,
    )
    return _run_native_streaming(
        table=table,
        plan=plan,
        config=config,
        column_order=classification.column_order,
        first_batch=first,
        rest_batches=execution_batches,
        sink=sink,
        streaming=streaming,
        table_kinds=table_kinds,
        resolved_substrate=resolved_substrate,
        explain_plan=explain_plan,
        execution_plan_decision=execution_plan_decision,
        source_schema=classification.schema,
        digest_state=digest_state,
    )


__all__ = [
    "ColumnState",
    "ExecutionDigestState",
    "PreflightColumnAccumulator",
    "PreflightResult",
    "RouteAdmission",
    "TypeFamily",
    "classify_and_preflight",
    "combine_column_digests",
    "resolve_admission",
    "resolve_column_state",
    "run_preflight",
    "run_widened_execution",
    "schema_drift_reason",
    "type_family",
]
