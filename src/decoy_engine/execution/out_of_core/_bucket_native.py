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

import pandas as pd
import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_PANDAS, RUST_COMPANION
from decoy_engine.execution.native._bucket_perturb_ext import native_bucket_perturb
from decoy_engine.execution.native._operator_config_rejections import (
    bucket_perturb_config_rejection,
)

if TYPE_CHECKING:
    from decoy_engine.execution.out_of_core._categorical_native import BoundIndexKernel
    from decoy_engine.plan._types import ColumnSeed

REASON_UNAVAILABLE = "index_extension_unavailable"
REASON_NOT_STRING = "bucket_perturb_source_not_string"
REASON_CONFIG_DECLINED = "bucket_perturb_config_declined"
REASON_OUT_OF_NATIVE_RANGE = "out_of_core_bucket_perturb_date_out_of_native_range"
_NO_BACKEND_RAN = "none"
_REASON_NO_NATIVE_CALLS = "no_native_kernel_calls"


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
class NativeBucket:
    """What the masking seam needs to run one bucket_perturb column on the compiled operator."""

    bound: BoundIndexKernel
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
                    code="out_of_core_bucket_perturb_column_missing",
                    message=(
                        f"bucket_perturb column {column!r} of table {decision.table!r} is missing "
                        "from a streamed batch; the run is aborted rather than skipping it."
                    ),
                )
            if batch.schema.field(idx).type != decision.pinned_type:
                raise ExecutionError(
                    code="out_of_core_bucket_perturb_source_type_drift",
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
        return None


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
        )
    return TableBucketPlan(decisions, bound)


# The widest span a bucket can reach from a date, in days, plus the day `normalize()` can drop.
# A date within this distance of a pandas Timestamp bound can step outside it during the native
# operator's bucket arithmetic, which pandas then wraps silently instead of raising.
_BUCKET_REACH_DAYS = {"week": 7 + 1, "month": 31 + 1, "quarter": 92 + 1}


def _guard_native_range(values: pa.Array, bucket: str, date_format: str, column: str) -> None:
    """Fail closed when any parseable date lies within one bucket of the pandas Timestamp range.

    `native_bucket_perturb` does its bucket arithmetic on pandas Timestamps (1677-09-21 to
    2262-04-11). Near either bound that arithmetic wraps to a wrong date with no exception, so
    the result must never be consumed. A date outside the range at all parses to NaT on both
    paths and passes through unchanged, so it needs no guard; the Python path covers the whole
    calendar range for the columns this refuses."""
    series = values.to_pandas()
    if pd.api.types.is_extension_array_dtype(series.dtype):
        series = series.astype(object)
    parsed = pd.to_datetime(series, format=date_format, errors="coerce")
    earliest, latest = parsed.min(), parsed.max()
    if pd.isna(earliest):
        return
    reach = pd.Timedelta(days=_BUCKET_REACH_DAYS[bucket])
    if earliest < pd.Timestamp.min + reach or latest > pd.Timestamp.max - reach:
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
) -> pa.Array:
    """Perturb one column slice with the compiled operator.

    There is no fallback: a kernel fault or an out-of-range date propagates and aborts the run,
    so a column never ends up half native and half Python. An empty or entirely null slice is
    answered here without a kernel call, as the null-typed array the pandas path yields for it;
    an all-unparseable slice passes its text through and stays a string array."""
    column = native.decision.column
    n = len(values)
    if n == 0 or values.null_count == n:
        return pa.nulls(n)
    array = values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values
    _guard_native_range(array, bucket, date_format, column)
    derive_calls: list[int] = []
    out = native_bucket_perturb(
        array,
        bucket=bucket,
        date_format=date_format,
        mask_key=mask_key,
        namespace=namespace,
        index_kernel=native.bound.kernel,
        native_threads=native.bound.native_threads,
        derive_calls=derive_calls,
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
