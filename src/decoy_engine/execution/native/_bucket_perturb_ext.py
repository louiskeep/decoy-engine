"""Native deterministic bucket_perturb, reusing `derive_index_batch`.

Phase 5 S-slate operator #1. The oracle (`transforms/bucket_perturb.py`)
snaps each parseable date to a deterministic position inside its time bucket
(ISO week / calendar month / calendar quarter): parse the string, find the
bucket start and size, draw a keyed within-bucket offset, add it, and
strftime back; null and unparseable rows pass through UNCHANGED. The keyed
offset is `int.from_bytes(derive(job_seed, namespace, canon(value))[:8],
"big") % bucket_size`, which is exactly what the compiled `derive_index_batch`
kernel computes byte-for-byte (KAT-pinned, `_index_ext.py`; canonicalization
shared via `generation.pool._canonicalize._canonicalize_source`), so this
operator reuses that kernel and adds NO new Rust.

Two parity facts drive the design (both proven by the acceptance differential):

- PANDAS is the parse/format authority, not Arrow. Arrow's datetime parser and
  formatter differ from pandas on permissive input, unsupported directives, and
  error-to-null, so an Arrow round-trip could turn an oracle-valid date into a
  native parse-fail (or vice versa). Parse with the SAME
  `pd.to_datetime(values, format=date_format, errors="coerce")` and format with
  the SAME `strftime` the oracle uses.

- The bucket size VARIES per row (leap years): week is always 7; month is
  28/29/30/31; quarter is 90/91/92 (leap Q1). `derive_index_batch` takes a
  scalar `pool_size`, so the parseable rows are GROUPED by their distinct
  bucket_size and one batch call is made per size, then the offsets are
  scattered back order-preservingly. Each row's offset is therefore drawn with
  `pool_size == its own bucket_size`, identical to the oracle's per-row `%
  bucket_size`.

v1 scope (see docs/plans/2026-09-21-native-bucket-perturb.md): STRING source,
EXPLICIT date_format, FULL-FRAME route only; every other shape declines to the
oracle at admission. The runtime invariants below mirror `native_categorical`
so a malformed compiled kernel fails HERE, coded and fail-closed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.generation.pool import GenerationError

if TYPE_CHECKING:
    from decoy_engine.execution.native._index_ext import IndexDerivationKernel

# Bucket names accepted on the native route (admission enforces membership).
_WEEK = "week"
_MONTH = "month"
_QUARTER = "quarter"

# Days in each quarter by 0-based quarter index, NON-leap. Q1 gains a day in a
# leap year (Feb 29); every other quarter is leap-invariant. This reproduces
# `_bucket_start_and_size`'s quarter day-count without a per-row date subtraction
# (Q1=Jan-Mar=90/91, Q2=Apr-Jun=91, Q3=Jul-Sep=92, Q4=Oct-Dec=92).
_QUARTER_SIZE_BASE = np.array([90, 91, 92, 92], dtype=np.int64)


def _bucket_start_and_size(
    parsed_valid: pd.DatetimeIndex, bucket: str
) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """Vectorized `(bucket_start, bucket_size)` for the parseable rows,
    reproducing the oracle's per-row `_bucket_start_and_size` exactly.

    `parsed_valid` is normalized to midnight first so the arithmetic is on the
    calendar date alone (the oracle takes `parsed.iloc[i].date()`); the bucket
    math never reads a time component, and the perturbed result is a pure date.
    """
    d = parsed_valid.normalize()
    if bucket == _WEEK:
        # ISO week starts Monday (weekday() == 0), size 7.
        start = d - pd.to_timedelta(d.dayofweek, unit="D")
        return start, np.full(len(d), 7, dtype=np.int64)
    if bucket == _MONTH:
        # First of the month; size = calendar.monthrange(...)[1] == days_in_month.
        start = d - pd.to_timedelta(d.day - 1, unit="D")
        return start, d.days_in_month.to_numpy().astype(np.int64)
    if bucket == _QUARTER:
        month = d.month.to_numpy()
        year = d.year.to_numpy()
        q_idx = (month - 1) // 3  # 0=Q1 .. 3=Q4
        q_start_month = q_idx * 3 + 1
        start = pd.DatetimeIndex(
            pd.to_datetime(
                {"year": year, "month": q_start_month, "day": np.ones(len(d), dtype=np.int64)}
            )
        )
        leap = (year % 4 == 0) & ((year % 100 != 0) | (year % 400 == 0))
        size = _QUARTER_SIZE_BASE[q_idx] + ((q_idx == 0) & leap).astype(np.int64)
        return start, size
    # Admission guarantees membership; this is the fail-closed second line.
    raise GenerationError(
        code="bucket_perturb_unrecognized_bucket",
        message=f"bucket_perturb reached the native kernel with unrecognized bucket {bucket!r}",
    )


def _grouped_offsets(
    valid_values: pa.Array,
    size: np.ndarray,
    *,
    mask_key: bytes,
    namespace: str,
    index_kernel: IndexDerivationKernel,
    native_threads: int | None,
) -> np.ndarray:
    """The keyed within-bucket offset per parseable row.

    Groups rows by their distinct `bucket_size` and calls `derive_index_batch`
    once per size with `pool_size == size`, scattering each group's uint64
    offsets back to their original positions. Byte-identical to the oracle's
    per-row `int.from_bytes(derive(...)[:8], "big") % bucket_size`.
    """
    n = len(size)
    offsets = np.empty(n, dtype=np.uint64)
    for s in np.unique(size):
        group_mask = size == s
        subset = valid_values.filter(pa.array(group_mask))
        idx = index_kernel.derive_index_batch(
            subset,
            mask_key=mask_key,
            namespace=namespace,
            pool_size=int(s),
            native_threads=native_threads,
        )
        # Runtime invariants on the kernel's own result (mirroring
        # `native_categorical`): the type check is FIRST so a non-`pa.Array`
        # result cannot leak an uncoded AttributeError.
        if not isinstance(idx, pa.Array) or idx.type != pa.uint64():
            got = idx.type if isinstance(idx, pa.Array) else type(idx).__name__
            raise GenerationError(
                code="index_batch_type_mismatch",
                message=f"derive_index_batch returned {got}, expected a uint64 Arrow array",
            )
        if len(idx) != len(subset):
            raise GenerationError(
                code="index_batch_length_mismatch",
                message=f"derive_index_batch returned {len(idx)} indices for {len(subset)} rows",
            )
        if idx.null_count != 0:
            # The subset is the non-null valid rows, so the kernel must return a
            # fully-valid offset column; a null here means a null-mask drift.
            raise GenerationError(
                code="index_batch_null_mask_mismatch",
                message="derive_index_batch returned a null offset for a non-null source row",
            )
        idx_np = idx.to_numpy(zero_copy_only=False)
        if idx_np.size and int(idx_np.max()) >= int(s):
            raise GenerationError(
                code="index_batch_out_of_bounds",
                message=f"derive_index_batch returned an offset >= bucket_size {int(s)}",
            )
        offsets[group_mask] = idx_np
    return offsets


def native_bucket_perturb(
    array: pa.Array | pa.ChunkedArray,
    *,
    bucket: str,
    date_format: str,
    mask_key: bytes | None,
    namespace: str,
    index_kernel: IndexDerivationKernel,
    native_threads: int | None = None,
) -> pa.Array:
    """Perturb a `pa.string()` date column onto the native full-frame lane.

    Parses each value with pandas (the parse authority), snaps parseable rows to
    a keyed within-bucket position via the compiled index kernel, and strftimes
    back with pandas (the format authority). Null and unparseable rows pass
    through UNCHANGED (the original string, never re-null/re-format). Output is
    pinned `pa.string()` per batch; the whole-column null-shape reconciliation to
    the oracle's data-dependent type happens at final assembly
    (`_shadow_coordinator._assemble_column`, the bucket_perturb branch).
    """
    if mask_key is None:  # pragma: no cover - require_mask_key never returns None
        raise AssertionError(
            "bucket_perturb reached with mask_key=None; require_mask_key always "
            "resolves a concrete key before the native route dispatches."
        )
    col = array.combine_chunks() if isinstance(array, pa.ChunkedArray) else array

    # Reproduce the oracle's own series preparation (`apply_bucket_perturb`): an
    # extension-dtype column is coerced to object first, then parsed with the
    # SAME pd.to_datetime call. `to_pandas()` for a pa.string() column yields
    # object dtype with None for nulls, matching the pandas adapter the oracle
    # reads through.
    series = col.to_pandas()
    if pd.api.types.is_extension_array_dtype(series.dtype):
        series = series.astype(object)
    parsed = pd.to_datetime(series, format=date_format, errors="coerce")

    null_mask = series.isna().to_numpy()
    # A parse-fail is a coerced NaT that was NOT already source-null; both pass
    # through unchanged, so the two are unioned into `valid`'s complement.
    parse_failed = parsed.isna().to_numpy() & ~null_mask
    valid = ~(null_mask | parse_failed)

    # Start from the ORIGINAL values so null + parse-fail rows are preserved
    # byte-for-byte; only the parseable rows are overwritten below.
    out = np.array(series.astype(object).to_numpy(copy=True), dtype=object)

    if valid.any():
        parsed_valid = pd.DatetimeIndex(parsed[valid].to_numpy())
        start, size = _bucket_start_and_size(parsed_valid, bucket)
        offsets = _grouped_offsets(
            col.filter(pa.array(valid)),
            size,
            mask_key=mask_key,
            namespace=namespace,
            index_kernel=index_kernel,
            native_threads=native_threads,
        )
        perturbed = start + pd.to_timedelta(offsets.astype(np.int64), unit="D")
        # pandas strftime (the format authority) over the perturbed dates, same
        # directive semantics as the oracle's per-row `perturbed.strftime(fmt)`.
        formatted = pd.DatetimeIndex(perturbed).strftime(date_format)
        out[valid] = np.asarray(formatted, dtype=object)

    return pa.array(out, type=pa.string())


__all__ = ["native_bucket_perturb"]
