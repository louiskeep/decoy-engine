"""Dtype byte-cost tables for the pure peak-memory estimator.

Extracted verbatim from `_mem_estimate.py` to keep that module under the
600-LOC orchestration cap (CLAUDE.md "Engineering best practices"). This is the
cohesive dtype-cost block: the fixed-width itemsize table, the variable-width
dtype set, the `is_fixed_width_dtype` classifier, and the per-cell string-object
cost the variable-width pricing uses. `_mem_estimate.py` re-imports the public
names so external callers keep resolving them there.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Fixed-width dtype cost table
# ---------------------------------------------------------------------------

# itemsize in bytes for every fixed-width dtype label `canonical_dtype_label`
# (internal/pandas_compat.py) can produce for a resident pandas Series, plus
# the plain numpy spellings a caller may pass directly. These are exactly
# `numpy.dtype(<label>).itemsize` -- not calibrated, just read off the numpy
# dtype table -- so a schema with more/fewer/different fixed-width columns
# is priced correctly without touching this module. The capitalized `Int64` /
# `boolean` / `Float64` spellings are pandas' NULLABLE extension dtypes, which a
# pandas-origin (e.g. Parquet) source restores via its `b"pandas"` sidecar and
# `canonical_dtype_label` passes through unchanged. They price at their base
# storage width, on the same Arrow-storage basis as the arrow-native labels
# above (an arrow `bool` is priced at 1 with its validity buffer omitted, so the
# nullable `boolean` is too). A column genuinely resident as a pandas extension
# array carries a full 1-byte-per-cell null mask on top, so this slightly
# under-counts that resident form (boolean ~2x, Int64 ~1/9); the gap is small
# against the module's GB-scale route thresholds and conservative K-constants.
# Omitting these labels mis-routed a nullable bool/int/float column to the
# string-width sampler, which crashed on a non-string cell.
_FIXED_WIDTH_DTYPE_BYTES: dict[str, int] = {
    "int64": 8,
    "uint64": 8,
    "float64": 8,
    "datetime64[ns]": 8,
    "datetime64": 8,
    "timedelta64[ns]": 8,
    "timedelta64": 8,
    "int32": 4,
    "uint32": 4,
    "float32": 4,
    "int16": 2,
    "uint16": 2,
    "int8": 1,
    "uint8": 1,
    "bool": 1,
    # pandas nullable extension dtypes (base storage width).
    "boolean": 1,
    "Int64": 8,
    "UInt64": 8,
    "Float64": 8,
    "Int32": 4,
    "UInt32": 4,
    "Float32": 4,
    "Int16": 2,
    "UInt16": 2,
    "Int8": 1,
    "UInt8": 1,
}

# dtype labels that are variable-width and therefore need a per-cell string
# cost rather than a table lookup. `canonical_dtype_label` emits "object" for
# both a masked source's inferred-string columns and pandas-3's `str` dtype
# (see its docstring); "string"/"string[pyarrow]" are the explicit pandas
# extension-dtype spellings. Anything not in this set and not in
# `_FIXED_WIDTH_DTYPE_BYTES` is an unrecognized dtype label -- `_column_bytes`
# fails closed on it rather than silently pricing it as free.
_VARIABLE_WIDTH_DTYPES = frozenset({"object", "string", "string[pyarrow]", "large_string"})


def is_fixed_width_dtype(dtype: str) -> bool:
    """Whether `dtype` prices as a fixed-width column (`_FIXED_WIDTH_DTYPE_BYTES`).

    Public so `_mem_estimate_schema`'s adapters can classify a profiled
    column's dtype without importing this module's private cost table
    directly.
    """
    return dtype in _FIXED_WIDTH_DTYPE_BYTES


# CPython's compact-ASCII str object carries a fixed 49-byte header
# regardless of length (measured: `sys.getsizeof("")` is 49 on CPython
# 3.10-3.13 x86-64; PEP 393's flexible string representation keeps the
# per-object header constant and adds 1 byte/char for the common
# latin1-storage case). The numpy/pandas "object" ndarray that holds a
# string stores an 8-byte PyObject* reference per cell on TOP of that
# (numpy's object-array docs: elements are references, not inline data),
# and THAT reference is never shared -- every cell needs its own slot in
# the array regardless of what it points to. The 49-byte header + payload,
# however, is NOT necessarily per-cell: when the SAME string object is
# reused across cells (pooling/interning, or a columnar engine's
# dictionary encoding for a low-cardinality column), that header and its
# character bytes are paid ONCE for the whole column, and every other cell
# just holds another 8-byte pointer to it. This module cannot know at
# estimate time whether a given column's runtime values will end up pooled
# -- that depends on value cardinality and on the specific execution
# path's copy behavior, neither of which a static schema description
# carries -- so it prices the full per-cell cost (pointer + header +
# payload) as a conservative default rather than assuming pooling that may
# not happen. This is precisely the accounting gap pandas' own
# `DataFrame.memory_usage(deep=True)` flag exists to close: the shallow
# (default) count only sees the 8-byte pointers and silently drops the
# string payload. See `K_FULL_FRAME_SLOPE`'s docstring for how this
# per-cell pricing interacts with the k-constant when a column IS pooled.
_STR_OBJECT_POINTER_BYTES = 8
_STR_OBJECT_HEADER_BYTES = 49
_STR_OBJECT_OVERHEAD_BYTES = _STR_OBJECT_POINTER_BYTES + _STR_OBJECT_HEADER_BYTES  # 57


__all__ = [
    "is_fixed_width_dtype",
]
