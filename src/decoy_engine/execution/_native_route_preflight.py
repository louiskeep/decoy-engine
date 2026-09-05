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

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._output_projection import known_output_columns

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.execution._native_route import NativeRouteReport
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._readers import LazySource

ColumnState = Literal["empty", "no_null", "partial_null", "all_null"]

# 1. The four-state resolver


def resolve_column_state(total_rows: int, null_count: int) -> ColumnState:
    """One column's global state from its accumulated counts (plan section 2).

    The four states are disjoint and exhaustive over `0 <= null_count <=
    total_rows`: `empty` short-circuits on row count alone (a zero-row
    column has no nulls to count), so the remaining three only apply to a
    non-empty column.
    """
    if total_rows == 0:
        return "empty"
    if null_count == 0:
        return "no_null"
    if null_count == total_rows:
        return "all_null"
    return "partial_null"


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
def _row(no_null: bool, partial_null: bool, all_null: bool, empty: bool) -> dict[ColumnState, bool]:
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


# 3. The source-snapshot digest codec

# A fixed, readable domain-separation constant (blake2b `key=`, <=64 bytes)
# plus an explicit version byte folded into every hasher -- so a future
# codec revision cannot collide with this one even if the framing bytes
# happen to overlap.
_DOMAIN_KEY = b"decoy-engine/native-route/source-snapshot-digest/v1"
_VERSION_BYTE = b"\x01"
_DIGEST_SIZE = 32


def _new_hasher() -> Any:
    h = hashlib.blake2b(digest_size=_DIGEST_SIZE, key=_DOMAIN_KEY)
    h.update(_VERSION_BYTE)
    return h


def _type_token(arrow_type: pa.DataType) -> str:
    """A short, unambiguous string identifying `arrow_type` for the digest
    header: unit + timezone for timestamp, signedness + width for integer,
    so a tz change or a width change changes the digest even if every value
    happens to coincide numerically."""
    if arrow_type == pa.utf8():
        return "utf8"
    if pa.types.is_boolean(arrow_type):
        return "bool"
    if pa.types.is_integer(arrow_type):
        sign = "i" if pa.types.is_signed_integer(arrow_type) else "u"
        return f"{sign}{arrow_type.bit_width}"
    if pa.types.is_timestamp(arrow_type):
        return f"ts:{arrow_type.unit}:{arrow_type.tz or ''}"
    raise AssertionError(
        f"no digest type token for {arrow_type!s}"
    )  # pragma: no cover - admitted-types-only


def _update_hashers_for_array(
    validity_hasher: Any, aux_hasher: Any, value_hasher: Any, array: pa.Array
) -> None:
    """Fold one batch of one column into THREE separate hashers so re-
    partitioning the same data into different batch sizes folds to
    identical byte streams (plan section 4's partition-independence). Each
    quantity gets its OWN stream (never interleaved with another per batch
    -- validity-then-values or lengths-then-data interleaved once per batch
    is NOT partition-independent: an N-batch run interleaves N times, a
    1-batch run once, even though each quantity's own stream alone is).
    `aux_hasher` carries utf8's per-row length stream (untouched for every
    other type); `value_hasher` carries the payload (utf8 raw bytes, or the
    fixed-width numeric/bool/timestamp-ticks values).

    Validity is one byte per row (`pc.is_valid`, not a packed bitmap): no
    padding-bit bookkeeping across a batch boundary, at the cost of 7
    wasted bits per row. Every branch reads through a pyarrow compute
    kernel (`fill_null`, `is_valid`, `to_numpy`, a cast), so `.offset` is
    always honored by pyarrow's own conversion, never re-derived by hand. A
    null-free batch skips `is_valid`/`fill_null` (their result would equal
    an all-true validity string and the unchanged array): measured to
    matter, since this runs on every admitted batch twice and most real
    columns are null-free.
    """
    no_nulls = array.null_count == 0
    if no_nulls:
        validity_hasher.update(b"\x01" * len(array))
    else:
        validity = pc.is_valid(array)  # type: ignore[attr-defined, unused-ignore]
        validity_hasher.update(validity.to_numpy(zero_copy_only=False).tobytes())
    arrow_type = array.type
    if arrow_type == pa.utf8():
        # Null payloads folded to "" (zero-length, so a null contributes
        # nothing beyond its already-hashed validity byte); lengths go to
        # their OWN stream so the value bytes are length-PREFIXED, not a
        # bare concatenation -- "ab"+"c" and "a"+"bc" must never collide.
        filled = array if no_nulls else pc.fill_null(array, "")
        n = len(filled)
        offsets_buf = filled.buffers()[1]
        offsets = np.frombuffer(offsets_buf, dtype=np.int32, count=n + 1, offset=filled.offset * 4)
        aux_hasher.update(np.diff(offsets).astype(">i4").tobytes())
        data_buf = filled.buffers()[2]
        start, end = int(offsets[0]), int(offsets[-1])
        if data_buf is not None and end > start:
            value_hasher.update(memoryview(data_buf)[start:end])
        return
    if pa.types.is_boolean(arrow_type):
        filled_bool = array if no_nulls else pc.fill_null(array, False)
        value_hasher.update(filled_bool.to_numpy(zero_copy_only=False).tobytes())
        return
    if pa.types.is_integer(arrow_type):
        filled_int = array if no_nulls else pc.fill_null(array, 0)
        value_hasher.update(filled_int.to_numpy(zero_copy_only=False).tobytes())
        return
    if pa.types.is_timestamp(arrow_type):
        filled_ts = array if no_nulls else pc.fill_null(array, pa.scalar(0, type=arrow_type))
        value_hasher.update(filled_ts.cast(pa.int64()).to_numpy(zero_copy_only=False).tobytes())
        return
    raise AssertionError(
        f"no digest encoder for {arrow_type!s}"
    )  # pragma: no cover - admitted-types-only


def _finalize_column_digest(
    validity_hasher: Any,
    aux_hasher: Any,
    value_hasher: Any,
    *,
    name: str,
    arrow_type: pa.DataType,
    total_rows: int,
) -> bytes:
    """One column's digest, framed ONCE (name, type token, row count) around
    the three whole-column hashes folded across every batch -- so a
    reordered schema, a renamed column, or a row-count mismatch changes the
    digest even if two columns' value streams happen to coincide."""
    framed = _new_hasher()
    name_bytes = name.encode("utf-8")
    framed.update(len(name_bytes).to_bytes(4, "big"))
    framed.update(name_bytes)
    token_bytes = _type_token(arrow_type).encode("utf-8")
    framed.update(len(token_bytes).to_bytes(4, "big"))
    framed.update(token_bytes)
    framed.update(total_rows.to_bytes(8, "big"))
    framed.update(validity_hasher.digest())
    framed.update(aux_hasher.digest())
    framed.update(value_hasher.digest())
    return framed.digest()


def combine_column_digests(column_digests: list[bytes]) -> bytes:
    """The whole-source digest: the frozen column ORDER folded in by simply
    updating in that order, so a reordered schema changes the result."""
    top = _new_hasher()
    for digest in column_digests:
        top.update(digest)
    return top.digest()


@dataclass
class PreflightColumnAccumulator:
    """Per-column running state for one pass: row/null counts for the
    four-state resolver, plus the three digest hashers folded batch by
    batch. O(1) memory per batch; nothing retains an array reference."""

    name: str
    arrow_type: pa.DataType
    total_rows: int = 0
    null_count: int = 0
    _validity_hasher: Any = field(default_factory=_new_hasher, repr=False)
    _aux_hasher: Any = field(default_factory=_new_hasher, repr=False)
    _value_hasher: Any = field(default_factory=_new_hasher, repr=False)

    def observe(self, array: pa.Array) -> None:
        self.total_rows += len(array)
        self.null_count += array.null_count
        _update_hashers_for_array(
            self._validity_hasher, self._aux_hasher, self._value_hasher, array
        )

    def state(self) -> ColumnState:
        return resolve_column_state(self.total_rows, self.null_count)

    def digest(self) -> bytes:
        return _finalize_column_digest(
            self._validity_hasher,
            self._aux_hasher,
            self._value_hasher,
            name=self.name,
            arrow_type=self.arrow_type,
            total_rows=self.total_rows,
        )


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
    return {name: by_name[name]["strategy"] for name in column_order}


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
    execution_batches = source.iter_batches(batch_rows)
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
