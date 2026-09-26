"""Native deterministic date_shift, reusing `derive_index_batch`.

The oracle (`execution/_strategies/_date_shift.py`) shifts each parseable date
by a keyed per-value offset in `[min_days, max_days]`:
`min_days + (int.from_bytes(derive(mask_key, namespace, canon(value))[:8],
"big") % range_size)`. With no `group_by` the digest input is the source value
itself, so the offset is exactly `derive_index_batch(source, pool_size=
range_size) + min_days`: the compiled kernel computes that reduction
byte-for-byte (KAT-pinned, `_index_ext.py`; canonicalization shared via
`generation.pool._canonicalize._canonicalize_source`). This operator therefore
reuses the audited HKDF-SHA256 -> HMAC envelope (`decoy-engine-native/src/
derive.rs`) and adds NO new Rust or crypto.

Established methodology: per-value keyed date shifting is the standard
de-identification technique (HIPAA Safe Harbor date-shift guidance: a
consistent keyed offset preserves intervals for the same source value). Parse
and format are delegated to pandas, the oracle's own authority, following the
`_bucket_perturb_ext.py` template: Arrow's datetime parser/formatter differs
from pandas on permissive input and unsupported directives, so the post-step
calls the SAME `pd.to_datetime(errors="coerce")`, `+ pd.to_timedelta(unit="D")`
and `.dt.strftime` sequence the oracle runs.

Row errors: a NON-null value that fails to parse under the explicit format is a
`format_error`. The oracle leaves the original value in its frame and records a
`RowError`; this operator does the same and returns the BATCH-LOCAL positions,
so the coordinator can rebase them to table-global indices. Dropping them would
let an unparseable raw value reach the main output with the job succeeding, so
the caller must route them (the coordinator does; see `_shadow_coordinator`).

v1 scope: STRING source, EXPLICIT `date_format` (no `%z`/`%Z`), no `group_by`,
integer `min_days`/`max_days` inside the pandas Timedelta range, FULL-FRAME
route only. Every other shape declines to the oracle at admission
(`date_shift_config_rejection`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.generation.pool import GenerationError

if TYPE_CHECKING:
    from decoy_engine.execution.native._index_ext import IndexDerivationKernel

# The oracle's defaults (`_date_shift.py`: `cfg.get("min_days", -365)` etc.).
DEFAULT_MIN_DAYS = -365
DEFAULT_MAX_DAYS = 365

# `pd.Timedelta.max.days`. A bound outside +/- this can never produce a valid
# shift in pandas, so admission declines it to the oracle (which raises); it
# also keeps `offset + min_days` far from int64 overflow on the native side.
MAX_ABS_SHIFT_DAYS = 106_751

# The oracle's `reason` string for a parse failure, reproduced verbatim so the
# native and oracle `RowError` records compare equal.
FORMAT_ERROR_REASON = "value is not a parseable date under date_shift"


def resolve_shift_range(min_days: int, max_days: int) -> tuple[int, int]:
    """`(min_days, range_size)` exactly as the oracle resolves them: swap an
    inverted range, then `range_size = max - min + 1` (always >= 1)."""
    if min_days > max_days:
        min_days, max_days = max_days, min_days
    return min_days, max_days - min_days + 1


def _derive_offsets(
    values: pa.Array,
    *,
    range_size: int,
    mask_key: bytes,
    namespace: str,
    index_kernel: IndexDerivationKernel,
    native_threads: int | None,
) -> np.ndarray:
    """The keyed `[0, range_size)` draw per (non-null, parseable) row, with the
    same runtime invariants `native_bucket_perturb` applies to the kernel's own
    result, so a malformed compiled kernel fails here, coded and fail-closed."""
    idx = index_kernel.derive_index_batch(
        values,
        mask_key=mask_key,
        namespace=namespace,
        pool_size=range_size,
        native_threads=native_threads,
    )
    # Type check FIRST so a non-`pa.Array` result cannot leak an uncoded
    # AttributeError from the checks below.
    if not isinstance(idx, pa.Array) or idx.type != pa.uint64():
        got = idx.type if isinstance(idx, pa.Array) else type(idx).__name__
        raise GenerationError(
            code="index_batch_type_mismatch",
            message=f"derive_index_batch returned {got}, expected a uint64 Arrow array",
        )
    if len(idx) != len(values):
        raise GenerationError(
            code="index_batch_length_mismatch",
            message=f"derive_index_batch returned {len(idx)} indices for {len(values)} rows",
        )
    if idx.null_count != 0:
        raise GenerationError(
            code="index_batch_null_mask_mismatch",
            message="derive_index_batch returned a null offset for a non-null source row",
        )
    idx_np = idx.to_numpy(zero_copy_only=False)
    if idx_np.size and int(idx_np.max()) >= range_size:
        raise GenerationError(
            code="index_batch_out_of_bounds",
            message=f"derive_index_batch returned an offset >= range_size {range_size}",
        )
    return idx_np


def native_date_shift(
    array: pa.Array | pa.ChunkedArray,
    *,
    min_days: int,
    max_days: int,
    date_format: str,
    mask_key: bytes | None,
    namespace: str,
    index_kernel: IndexDerivationKernel,
    native_threads: int | None = None,
) -> tuple[pa.Array, tuple[int, ...]]:
    """Shift a `pa.string()` date column on the native full-frame lane.

    Returns `(out, format_error_positions)`: `out` is pinned `pa.string()` (the
    whole-column null-shape reconciliation to the oracle's data-dependent type
    happens at final assembly, `_shadow_assembly.assemble_column`), and
    `format_error_positions` are the 0-based positions WITHIN `array` of the
    non-null values that failed to parse. Null and unparseable rows keep their
    original value, as in the oracle.
    """
    if mask_key is None:  # pragma: no cover - require_mask_key never returns None
        raise AssertionError(
            "date_shift reached with mask_key=None; require_mask_key always "
            "resolves a concrete key before the native route dispatches."
        )
    min_days, range_size = resolve_shift_range(min_days, max_days)
    col = array.combine_chunks() if isinstance(array, pa.ChunkedArray) else array

    # The oracle's own series preparation: an extension dtype is coerced to
    # object before parsing. `to_pandas()` of a pa.string() column is object
    # dtype with None for nulls, matching the frame the oracle reads.
    series = col.to_pandas()
    if pd.api.types.is_extension_array_dtype(series.dtype):
        series = series.astype(object)
    parsed = pd.to_datetime(series, format=date_format, errors="coerce")
    unusable = parsed.isna().to_numpy()
    source_null = series.isna().to_numpy()

    # Unusable rows get a zero shift, exactly as the oracle's per-row loop does,
    # so the vectorized add below is the same operation over the same inputs.
    shifts = np.zeros(len(series), dtype=np.int64)
    usable = ~unusable
    if usable.any():
        offsets = _derive_offsets(
            col.filter(pa.array(usable)),
            range_size=range_size,
            mask_key=mask_key,
            namespace=namespace,
            index_kernel=index_kernel,
            native_threads=native_threads,
        )
        shifts[usable] = offsets.astype(np.int64) + min_days

    shifted = parsed + pd.to_timedelta(shifts, unit="D")
    formatted = shifted.dt.strftime(date_format)

    out = np.array(series.astype(object).to_numpy(copy=True), dtype=object)
    if usable.any():
        out[usable] = np.asarray(formatted.to_numpy()[usable], dtype=object)

    format_errors = tuple(int(i) for i in np.flatnonzero(unusable & ~source_null))
    return pa.array(out, type=pa.string()), format_errors


__all__ = [
    "DEFAULT_MAX_DAYS",
    "DEFAULT_MIN_DAYS",
    "FORMAT_ERROR_REASON",
    "MAX_ABS_SHIFT_DAYS",
    "native_date_shift",
    "resolve_shift_range",
]
