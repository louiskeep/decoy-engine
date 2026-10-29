"""Native routing for the out-of-core deterministic `categorical` mask.

The out-of-core route remaps categorical columns in a per-value Python loop
(`_mask_group_b.categorical_array`, which stays the reference and the declined path). For
columns the compiled index kernel handles identically, the Group (b) adapter calls the SAME
`native_categorical` operator the native-chunked route uses, so no keyed draw or weight-CDF
selection is re-implemented here.

Admission is the two-stage predicate the native-chunked route applies
(`prepare_chunked_categoricals`): stage A, the config (`prepare_categorical`), and stage B,
a source that is exactly `pa.string()` (`source_is_string`). The decision is made once per
table before the first batch and is immutable for the run; a native column is held to its
pinned source type on every batch and aborts the run on drift.

Reason codes carry the strategy outcome, the column name and the Arrow type only, never a
cell, key or category value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution.native._categorical_ext import native_categorical
from decoy_engine.execution.native._categorical_prepared import (
    PreparedCategorical,
    prepare_categorical,
    source_is_string,
)

if TYPE_CHECKING:
    from decoy_engine.execution.native._index_ext import IndexDerivationKernel
    from decoy_engine.plan._types import ColumnSeed

REASON_UNAVAILABLE = reasons.route.INDEX_EXTENSION_UNAVAILABLE
REASON_NOT_STRING = reasons.route.CATEGORICAL_SOURCE_NOT_STRING
_NO_BACKEND_RAN = "none"
_REASON_NO_NATIVE_CALLS = reasons.route.NO_NATIVE_KERNEL_CALLS


@dataclass(frozen=True)
class BoundIndexKernel:
    """The compiled index kernel, loaded once for the run, and the thread budget it runs with.

    `native_threads=None` leaves the budget to the compiled kernel's own default."""

    kernel: IndexDerivationKernel
    native_threads: int | None = None


def decline_reason(
    column: str, schema: pa.Schema, seed: ColumnSeed
) -> tuple[PreparedCategorical | None, str | None]:
    """`(prepared, None)` when `column` can run on the compiled operator, else `(None, reason)`.

    Stage A reads the config alone. A NaN, infinite or overflowing weight sum escapes the
    oracle's CDF builder as a bare ValueError or OverflowError instead of a coded error; it is
    declined here (as the chunked wrapper does) so it never reaches native, and the Python
    path then raises the oracle's own error where the oracle would."""
    try:
        prepared, reason = prepare_categorical(
            column,
            deterministic=seed.deterministic,
            namespace=seed.namespace,
            provider_config=provider_config_to_dict(seed.provider_config),
        )
    except (ValueError, OverflowError):
        return None, f"categorical_weights_unbuildable_cdf:{column}"
    if prepared is None:
        return None, reason
    if not source_is_string(schema, column):
        return None, f"{REASON_NOT_STRING}:{column}:{schema.field(column).type}"
    return prepared, None


@dataclass
class CategoricalColumnDecision:
    """The backend one categorical column takes for the whole run, and what it executed.

    `pinned_type` is the source Arrow type a native column is held to. `native_calls` counts
    the index-kernel invocations over the WHOLE column: an empty or all-null batch makes none
    and must not erase an earlier one."""

    table: str
    column: str
    backend: str
    reason: str | None
    pinned_type: pa.DataType
    prepared: PreparedCategorical | None = None
    rows: int = 0
    native_calls: int = 0

    @property
    def native(self) -> bool:
        return self.backend == RUST_COMPANION

    def entry(self) -> dict[str, Any]:
        ran = self.native_calls > 0 if self.native else self.rows > 0
        reason = self.reason or (None if ran else _REASON_NO_NATIVE_CALLS)
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
class NativeCategorical:
    """What `mask_column` needs to run one categorical column on the compiled operator."""

    bound: BoundIndexKernel
    decision: CategoricalColumnDecision


class TableCategoricalPlan:
    """The immutable per-column decisions for one table, and the per-batch gate."""

    def __init__(
        self, decisions: Mapping[str, CategoricalColumnDecision], bound: BoundIndexKernel | None
    ) -> None:
        self._decisions = dict(decisions)
        self._bound = bound

    @property
    def decisions(self) -> Mapping[str, CategoricalColumnDecision]:
        return self._decisions

    def gate(self, column: str, batch: pa.RecordBatch) -> NativeCategorical | None:
        """The native handle for `column` in `batch`, or None when it stays on Python.

        A native column is held to its pinned source type: a batch that lacks it or carries
        another type aborts the run, so a column never changes path mid-stream."""
        decision = self._decisions.get(column)
        if decision is None:
            return None
        idx = batch.schema.get_field_index(column)
        if decision.native:
            if idx < 0:
                raise ExecutionError(
                    code=reasons.runtime.OUT_OF_CORE_CATEGORICAL_COLUMN_MISSING,
                    message=(
                        f"categorical column {column!r} of table {decision.table!r} is missing "
                        "from a streamed batch; the run is aborted rather than skipping it."
                    ),
                )
            if batch.schema.field(idx).type != decision.pinned_type:
                raise ExecutionError(
                    code=reasons.runtime.OUT_OF_CORE_CATEGORICAL_SOURCE_TYPE_DRIFT,
                    message=(
                        f"categorical column {column!r} of table {decision.table!r} was pinned "
                        f"as {decision.pinned_type} but a batch carries "
                        f"{batch.schema.field(idx).type}; the run is aborted rather than "
                        "changing path mid-column."
                    ),
                )
        if idx >= 0:
            decision.rows += batch.num_rows
        if decision.native and self._bound is not None:
            return NativeCategorical(self._bound, decision)
        return None


def decide_table(
    table_name: str,
    schema: pa.Schema,
    seeds: Mapping[str, ColumnSeed],
    bound: BoundIndexKernel | None,
) -> TableCategoricalPlan:
    """Decide every categorical column of one table, before its first batch is read."""
    decisions: dict[str, CategoricalColumnDecision] = {}
    for column, seed in seeds.items():
        prepared: PreparedCategorical | None = None
        reason: str | None = REASON_UNAVAILABLE
        if bound is not None:
            prepared, reason = decline_reason(column, schema, seed)
        decisions[column] = CategoricalColumnDecision(
            table_name,
            column,
            RUST_COMPANION if prepared is not None else PYTHON_SCALAR,
            reason,
            schema.field(column).type,
            prepared,
        )
    return TableCategoricalPlan(decisions, bound)


def native_categorical_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    native: NativeCategorical,
    mask_key: bytes,
    namespace: str,
) -> pa.Array:
    """Remap one column slice with the compiled operator.

    There is no fallback: a kernel fault propagates and aborts the run, so a column never
    ends up half native and half Python. `native_categorical` derives its indices
    unconditionally, so an empty or entirely null slice is answered here without a kernel
    call; its result is the null-typed array the Python loop yields for the same slice, and
    it adds nothing to the column's call count."""
    n = len(values)
    if n == 0 or values.null_count == n:
        return pa.nulls(n)
    prepared = native.decision.prepared
    if prepared is None:  # pragma: no cover - a native decision always carries its artifact
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_CATEGORICAL_UNPREPARED,
            message="a native categorical column reached the kernel without a prepared pool.",
        )
    native.decision.native_calls += 1
    return native_categorical(
        values,
        categories=prepared.categories,
        cdf=prepared.cdf,
        mask_key=mask_key,
        namespace=namespace,
        index_kernel=native.bound.kernel,
        native_threads=native.bound.native_threads,
    )


__all__ = [
    "REASON_NOT_STRING",
    "REASON_UNAVAILABLE",
    "BoundIndexKernel",
    "CategoricalColumnDecision",
    "NativeCategorical",
    "TableCategoricalPlan",
    "decide_table",
    "decline_reason",
    "native_categorical_array",
]
