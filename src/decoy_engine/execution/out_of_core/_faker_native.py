"""Native routing for the out-of-core deterministic-REUSE `faker` payload (B4).

The out-of-core route declined faker entirely before this slice. It now runs the
ONE parity-provable variant: deterministic REUSE, an exact-`string` source, a
person-name provider on the native allowlist, a namespace, and an explicit
pool_size. Selection reuses the SAME compiled index kernel
(`sample_faker_array`) the sequential and native-chunked routes call, drawing
from a pool the run-scoped `OocFakerPoolContext` (B3) builds once and reuses
across batches. No keyed draw or pool-selection algorithm is re-implemented here.

Unlike categorical, faker has no self-contained per-value Python reference (a
pool must be built from the provider registry), so there is no decline-to-Python
inside the route. Two things keep that safe:

- The compiled index kernel's pure-Python reference (`reference_index_derivation`)
  is byte-identical to the compiled one (INDEX_KAT), so a box without the
  companion still selects correctly; it is recorded as a non-compiled backend.
- The admitted scope is exact-`string`. A non-`string` source (int, bool,
  large_string, float, temporal) still masks correctly on full-frame/sequential,
  so it must REROUTE, never be admitted here and then abort. The compat gate
  declines it at routing when the schema is in hand (`_compat.
  _faker_conditional_rejection` with `source_types`), so an auto-routed job
  reroutes to full-frame/sequential with no regression; a forced out-of-core job
  refuses at the runner preflight, before any scratch or sink write. `decide_table`
  below keeps the same check as a fail-closed backstop, so a raw source value is
  never leaked even if a non-string column slips past both gates; the runner's
  all-or-nothing publish then aborts any sink and releases spill.

Reason codes carry the strategy outcome, the column name and the Arrow type
only, never a cell, key, or generated value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_POOL_SELECT
from decoy_engine.execution.native._operator_step import sample_faker_array
from decoy_engine.execution.native._real_type_admission import (
    C1_PROVIDER_ALLOWLIST,
    string_source_type_rejection,
)
from decoy_engine.execution.native._requirements import faker_pool_precondition_met_seed

if TYPE_CHECKING:
    from decoy_engine.execution.native._index_ext import IndexDerivationKernel
    from decoy_engine.execution.out_of_core._faker_pool import OocFakerPoolContext
    from decoy_engine.generation.pool._value_pool import ValuePool
    from decoy_engine.plan._types import ColumnSeed

_NO_BACKEND_RAN = "none"
_REASON_NO_SELECT_CALLS = reasons.route.NO_NATIVE_KERNEL_CALLS


@dataclass(frozen=True)
class BoundFakerKernel:
    """The index kernel one run selects faker pools with, and the thread budget.

    `compiled` is True for the Rust companion and False for the pure-Python
    reference (`reference_index_derivation`); both derive byte-identical indices
    (INDEX_KAT), so the flag changes only the recorded backend, never the output.
    """

    kernel: IndexDerivationKernel
    native_threads: int | None
    compiled: bool


def decline_reason(column: str, schema: pa.Schema, seed: ColumnSeed) -> str | None:
    """None when `column` can run the deterministic-REUSE native faker payload, else a reason.

    The admitted subset is the exact one the sequential and chunked routes gate
    on: a person-name provider on `C1_PROVIDER_ALLOWLIST`, the deterministic
    REUSE precondition (`deterministic`, `cardinality_mode=reuse`, a namespace,
    an explicit pool_size), and an exact-`string` source. The non-deterministic
    (positional) REUSE leg needs the global row offset and is out of this slice's
    scope; UNIQUE/MATCH/SCALE need whole-column RNG state. Those all decline here.
    """
    if seed.provider not in C1_PROVIDER_ALLOWLIST:
        return f"faker_provider_not_native:{column}:{seed.provider}"
    if not faker_pool_precondition_met_seed(seed):
        return f"faker_not_deterministic_reuse:{column}"
    return string_source_type_rejection("faker", column, schema)


@dataclass
class FakerColumnDecision:
    """The backend one faker column takes for the whole run, and what it executed.

    `pinned_type` is the source Arrow type a native column is held to. `pool` is
    the ONE pool built for this column, reused across every batch. `select_calls`
    counts the index-kernel invocations over the WHOLE column: an empty batch
    makes none and must not erase an earlier one.
    """

    table: str
    column: str
    backend: str
    reason: str | None
    pinned_type: pa.DataType
    namespace: str | None = None
    pool: ValuePool | None = None
    rows: int = 0
    select_calls: int = 0

    @property
    def native(self) -> bool:
        return self.pool is not None

    def entry(self) -> dict[str, Any]:
        ran = self.select_calls > 0
        reason = self.reason or (None if ran else _REASON_NO_SELECT_CALLS)
        entry: dict[str, Any] = {
            "table": self.table,
            "column": self.column,
            "path": "mask_batch",
            "executed_backend": self.backend if ran else _NO_BACKEND_RAN,
        }
        if reason is not None:
            entry["reason"] = reason
        return entry


@dataclass(frozen=True)
class NativeFaker:
    """What `mask_column` needs to select one faker column from its prepared pool."""

    bound: BoundFakerKernel
    decision: FakerColumnDecision


class TableFakerPlan:
    """The immutable per-column faker decisions for one table, and the per-batch gate."""

    def __init__(
        self, decisions: Mapping[str, FakerColumnDecision], bound: BoundFakerKernel | None
    ) -> None:
        self._decisions = dict(decisions)
        self._bound = bound

    @property
    def decisions(self) -> Mapping[str, FakerColumnDecision]:
        return self._decisions

    def gate(self, column: str, batch: pa.RecordBatch) -> NativeFaker | None:
        """The native handle for faker `column` in `batch`, or None when not planned.

        A planned faker column is held to its pinned source type: a batch that
        lacks it or carries another type aborts the run, so a column never
        changes path mid-stream.
        """
        decision = self._decisions.get(column)
        if decision is None or not decision.native:
            return None
        idx = batch.schema.get_field_index(column)
        if idx < 0:
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FAKER_COLUMN_MISSING,
                message=(
                    f"faker column {column!r} of table {decision.table!r} is missing from a "
                    "streamed batch; the run is aborted rather than skipping it."
                ),
            )
        if batch.schema.field(idx).type != decision.pinned_type:
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FAKER_SOURCE_TYPE_DRIFT,
                message=(
                    f"faker column {column!r} of table {decision.table!r} was pinned as "
                    f"{decision.pinned_type} but a batch carries {batch.schema.field(idx).type}; "
                    "the run is aborted rather than changing path mid-column."
                ),
            )
        decision.rows += batch.num_rows
        if self._bound is None:  # pragma: no cover - a native decision always carries a kernel
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FAKER_INDEX_UNAVAILABLE,
                message=(
                    f"faker column {column!r} was decided native but no index kernel is bound."
                ),
            )
        return NativeFaker(self._bound, decision)


def decide_table(
    table_name: str,
    schema: pa.Schema,
    seeds: Mapping[str, ColumnSeed],
    bound: BoundFakerKernel | None,
    faker_pool: OocFakerPoolContext | None,
) -> TableFakerPlan:
    """Decide every faker column of one table, before its first batch is read.

    Builds the ONE pool per admitted column through the run-scoped context (which
    reserves its memory before allocating the provider and reuses an identical
    pool across tables). A faker column whose shape or source the admitted subset
    excludes aborts the run fail-closed: faker has no in-route Python reference,
    so leaving such a column unmasked would leak its raw source value. The compat
    gate reroutes a non-string faker source at routing (auto) and the runner
    preflight refuses it (forced), both with the schema in hand, so this path is
    the defensive backstop that fires only if a non-string column slips past both.
    """
    decisions: dict[str, FakerColumnDecision] = {}
    for column, seed in seeds.items():
        pinned_type = schema.field(column).type
        if bound is None or faker_pool is None:
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FAKER_POOL_UNSUPPORTED,
                message=(
                    f"faker column {column!r} of table {table_name!r} reached the out-of-core "
                    "mask planner without a pool context or index kernel; the run is aborted."
                ),
            )
        reason = decline_reason(column, schema, seed)
        if reason is not None:
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FAKER_SOURCE_TYPE_UNSUPPORTED,
                message=(
                    f"faker column {column!r} of table {table_name!r} is not the admitted "
                    f"deterministic-REUSE exact-string shape ({reason}); the out-of-core route "
                    "has no reference faker path for it, so the run is aborted fail-closed rather "
                    "than leaking the raw source value."
                ),
            )
        # decline_reason returned None, so the provider is on the allowlist and
        # therefore not None (a None provider is never in the allowlist). Narrow
        # it with a fail-closed guard rather than an assert that `-O` could strip.
        provider = seed.provider
        if provider is None:  # pragma: no cover - unreachable past decline_reason
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FAKER_POOL_UNSUPPORTED,
                message=f"faker column {column!r} of table {table_name!r} has no provider.",
            )
        prepared = faker_pool.prepare(
            provider=provider,
            namespace=seed.namespace,
            cfg=provider_config_to_dict(seed.provider_config),
            plan_pool_size=seed.pool_size,
        )
        pool = faker_pool.get_pool(prepared)
        decisions[column] = FakerColumnDecision(
            table=table_name,
            column=column,
            backend=RUST_POOL_SELECT if bound.compiled else PYTHON_SCALAR,
            reason=None,
            pinned_type=pinned_type,
            namespace=seed.namespace,
            pool=pool,
        )
    return TableFakerPlan(decisions, bound)


def native_faker_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    native: NativeFaker,
    mask_key: bytes,
    namespace: str,
) -> pa.Array:
    """Select one column slice's faker values from the prepared pool.

    There is no fallback: a kernel fault propagates and aborts the run, so a
    column never ends up half native and half something else. An empty slice is
    answered here as an empty string array without a kernel call and adds nothing
    to the column's call count; an all-null slice still goes through the kernel so
    its string typing and null restoration match the oracle exactly.
    """
    n = len(values)
    if n == 0:
        return pa.array([], type=pa.string())
    pool = native.decision.pool
    if pool is None:  # pragma: no cover - a native decision always carries its pool
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_FAKER_UNPREPARED,
            message="a native faker column reached the kernel without a prepared pool.",
        )
    native.decision.select_calls += 1
    return sample_faker_array(
        values,
        pool=pool,
        namespace=namespace,
        mask_key=mask_key,
        index_kernel=native.bound.kernel,
        native_threads=native.bound.native_threads,
    )


__all__ = [
    "BoundFakerKernel",
    "FakerColumnDecision",
    "NativeFaker",
    "TableFakerPlan",
    "decide_table",
    "decline_reason",
    "native_faker_array",
]
