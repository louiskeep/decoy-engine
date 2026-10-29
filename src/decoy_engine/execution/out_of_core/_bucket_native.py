"""Native routing for the out-of-core `bucket_perturb` mask.

The out-of-core route perturbs dates by running `apply_bucket_perturb` over a pandas Series
(`_mask_group_c._bucket_perturb_array`, which stays the reference and the declined path). For
columns the compiled index kernel handles identically, that seam calls the SAME
`native_bucket_perturb` operator the native-chunked route uses, so no keyed offset or bucket
arithmetic is re-implemented here.

Eligibility is the one resolver both native boundaries already share
(`bucket_perturb_config_rejection`). It is handed the captured source schema as a zero-row table,
so the Arrow source type is authoritative: a non-string, lazy non-string or profile-disagreeing
source declines at prep. The decision is made once per table before the first batch and is
immutable for the run; a native column is held to its pinned source type on every batch and aborts
the run on drift.

This module changes the compute path only. Config validation and its errors keep their existing
stages and order (OOC compatibility, then `preflight_group_c`, then the namespace check at the
masking seam); a native choice is consulted only for a config that clears all three.

Reason codes carry the strategy outcome, the column name and the Arrow type only, never a cell,
key or date.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_PANDAS, RUST_COMPANION
from decoy_engine.execution.native._bucket_perturb_ext import (
    native_bucket_perturb,
    native_range_exceeded,
)
from decoy_engine.execution.native._operator_config_rejections import (
    bucket_perturb_config_rejection,
)
from decoy_engine.transforms.date_preserve import PreserveSet, preserve_set_from_config

if TYPE_CHECKING:
    from decoy_engine.execution.out_of_core._categorical_native import BoundIndexKernel
    from decoy_engine.plan._types import ColumnSeed

REASON_UNAVAILABLE = reasons.route.INDEX_EXTENSION_UNAVAILABLE
REASON_NOT_STRING = reasons.route.BUCKET_PERTURB_SOURCE_NOT_STRING
REASON_CONFIG_DECLINED = reasons.route.BUCKET_PERTURB_CONFIG_DECLINED
REASON_OUT_OF_NATIVE_RANGE = reasons.runtime.OUT_OF_CORE_BUCKET_PERTURB_DATE_OUT_OF_NATIVE_RANGE
_NO_BACKEND_RAN = "none"
_REASON_NO_NATIVE_CALLS = reasons.route.NO_NATIVE_KERNEL_CALLS


def decline_reason(table: str, column: str, schema: pa.Schema, seed: ColumnSeed) -> str | None:
    """`None` when `column` can run on the compiled operator, else the value-free decline reason.

    The shared resolver keeps its own coded verdict. A non-string source is reported with its
    Arrow type; every other rejection (the tz directive is the one that clears the earlier
    validation stages) keeps only the resolver's code, so the reason never carries a format."""
    rejection = bucket_perturb_config_rejection(
        column,
        table,
        None,
        namespace=seed.namespace,
        provider_config=provider_config_to_dict(seed.provider_config),
        resident_sources={table: schema.empty_table()},
    )
    if rejection is None:
        return None
    code = rejection.split(":", 1)[0]
    if code == REASON_NOT_STRING:
        return rejection
    return f"{REASON_CONFIG_DECLINED}:{column}:{code}"


@dataclass
class BucketColumnDecision:
    """The backend one bucket_perturb column takes for the whole run, and what it executed.

    `pinned_type` is the source Arrow type a native column is held to. `native_calls` counts the
    compiled batch calls over the WHOLE column: an empty, all-null or all-unparseable batch makes
    none and must not erase an earlier one."""

    table: str
    column: str
    backend: str
    reason: str | None
    pinned_type: pa.DataType
    rows: int = 0
    native_calls: int = 0
    # D1c: the column opted into `preserve_values`, so its entry also reports how many sentinel
    # rows it passed through. Absent from the entry otherwise.
    counts_preserved: bool = False
    preserved_rows: int = 0

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
        if self.counts_preserved:
            entry["preserved_rows"] = self.preserved_rows
        return entry


@dataclass(frozen=True)
class NativeBucket:
    """What the masking seam needs for one bucket_perturb column.

    `bound` is the compiled index kernel, or None for a column that stays on Python but still
    reports its D1c sentinel count through `decision`."""

    bound: BoundIndexKernel | None
    decision: BucketColumnDecision


class TableBucketPlan:
    """The immutable per-column decisions for one table, and the per-batch gate."""

    def __init__(
        self, decisions: Mapping[str, BucketColumnDecision], bound: BoundIndexKernel | None
    ) -> None:
        self._decisions = dict(decisions)
        self._bound = bound

    @property
    def decisions(self) -> Mapping[str, BucketColumnDecision]:
        return self._decisions

    def gate(self, column: str, batch: pa.RecordBatch) -> NativeBucket | None:
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
                    code=reasons.runtime.OUT_OF_CORE_BUCKET_PERTURB_COLUMN_MISSING,
                    message=(
                        f"bucket_perturb column {column!r} of table {decision.table!r} is missing "
                        "from a streamed batch; the run is aborted rather than skipping it."
                    ),
                )
            if batch.schema.field(idx).type != decision.pinned_type:
                raise ExecutionError(
                    code=reasons.runtime.OUT_OF_CORE_BUCKET_PERTURB_SOURCE_TYPE_DRIFT,
                    message=(
                        f"bucket_perturb column {column!r} of table {decision.table!r} was pinned "
                        f"as {decision.pinned_type} but a batch carries "
                        f"{batch.schema.field(idx).type}; the run is aborted rather than "
                        "changing path mid-column."
                    ),
                )
        if idx >= 0:
            decision.rows += batch.num_rows
        if decision.native and self._bound is not None:
            return NativeBucket(self._bound, decision)
        if decision.counts_preserved:
            return NativeBucket(None, decision)
        return None


def _has_preserve_values(seed: ColumnSeed) -> bool:
    """Whether the column configures a valid, non-empty `preserve_values`.

    An invalid one does not count: it raises at the masking seam, so the column never runs."""
    try:
        return preserve_set_from_config(provider_config_to_dict(seed.provider_config)) is not None
    except ValueError:
        return False


def decide_table(
    table_name: str,
    schema: pa.Schema,
    seeds: Mapping[str, ColumnSeed],
    bound: BoundIndexKernel | None,
) -> TableBucketPlan:
    """Decide every bucket_perturb column of one table, before its first batch is read."""
    decisions: dict[str, BucketColumnDecision] = {}
    for column, seed in seeds.items():
        reason = (
            decline_reason(table_name, column, schema, seed)
            if bound is not None
            else REASON_UNAVAILABLE
        )
        decisions[column] = BucketColumnDecision(
            table_name,
            column,
            RUST_COMPANION if reason is None else PYTHON_PANDAS,
            reason,
            schema.field(column).type,
            counts_preserved=_has_preserve_values(seed),
        )
    return TableBucketPlan(decisions, bound)


def _guard_native_range(values: pa.Array, bucket: str, date_format: str, column: str) -> None:
    """Fail closed when a parseable date lies within one bucket of the native operator's range.

    The out-of-core route streams and cannot re-buffer a column for Python, so it aborts where
    the sequential route declines; the predicate itself is shared (`native_range_exceeded`)."""
    if native_range_exceeded(values, bucket, date_format):
        raise ExecutionError(
            code=REASON_OUT_OF_NATIVE_RANGE,
            message=(
                f"bucket_perturb column {column!r} holds a date within one {bucket} of the range "
                "the native operator can compute (1677-09-21 to 2262-04-11); the run is aborted "
                "rather than consuming a possibly wrapped result. Run without the compiled "
                "companion to mask the full calendar range."
            ),
        )


def native_bucket_perturb_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    native: NativeBucket,
    mask_key: bytes,
    namespace: str,
    bucket: str,
    date_format: str,
    preserve: PreserveSet | None = None,
) -> pa.Array:
    """Perturb one column slice with the compiled operator.

    There is no fallback: a kernel fault or an out-of-range date propagates and aborts the run,
    so a column never ends up half native and half Python. An empty or entirely null slice is
    answered here without a kernel call, as the null-typed array the pandas path yields for it;
    an all-unparseable slice passes its text through and stays a string array.

    With `preserve`, the sentinel rows are found first and the range guard sees only the rest, so
    a sentinel next to the pandas bounds does not abort the run; those rows then bypass the
    kernel entirely."""
    bound = native.bound
    if bound is None:  # pragma: no cover - the seam routes a Python column elsewhere
        raise AssertionError("native_bucket_perturb_array called for a column without a kernel.")
    column = native.decision.column
    n = len(values)
    if n == 0 or values.null_count == n:
        return pa.nulls(n)
    array = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
    preserved = preserve.mask(array.to_pandas()) if preserve is not None else None
    if preserved is not None and preserved.any():
        native.decision.preserved_rows += int(preserved.sum())
        guarded = array.filter(pa.array(~preserved))
    else:
        guarded = array
    _guard_native_range(guarded, bucket, date_format, column)
    derive_calls: list[int] = []
    out = native_bucket_perturb(
        array,
        bucket=bucket,
        date_format=date_format,
        mask_key=mask_key,
        namespace=namespace,
        index_kernel=bound.kernel,
        native_threads=bound.native_threads,
        derive_calls=derive_calls,
        preserved=preserved,
    )
    native.decision.native_calls += sum(derive_calls)
    return out


__all__ = [
    "REASON_CONFIG_DECLINED",
    "REASON_NOT_STRING",
    "REASON_OUT_OF_NATIVE_RANGE",
    "REASON_UNAVAILABLE",
    "BucketColumnDecision",
    "NativeBucket",
    "TableBucketPlan",
    "decide_table",
    "decline_reason",
    "native_bucket_perturb_array",
]
