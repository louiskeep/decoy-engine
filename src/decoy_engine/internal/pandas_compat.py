"""Cross-version pandas compatibility helpers.

CONTRACT (audit M5, 2026-06-12): dtype labels emitted into persisted
payloads (ColumnProfile, distribution snapshots) feed user-held snapshot
digests, so they must not drift when the pandas version changes. pandas
3 made ``str`` the default inferred string dtype where pandas 1.5-2.x
inferred ``object``; reporting the new label verbatim silently broke
every pre-existing baseline digest.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any, Final, NoReturn

import pandas as pd
import pyarrow as pa

# pandas-3 default datetime inference resolution is 'us' where 1.5-2.x
# always produced 'ns'. Resolution is uniform within one pandas version,
# so cross-frame drift comparison loses nothing by pinning the label.
_DATETIME_RES = re.compile(r"^datetime64\[(s|ms|us|ns)(, .+)?\]\Z")


def canonical_dtype_label(dtype: Any) -> str:
    """Stable dtype label across pandas major versions.

    Two pandas-3 inference changes are normalized to their historical
    labels so digests stay byte-stable across the 2.x -> 3.x boundary:
    the DEFAULT inferred string dtype (``str`` -> ``object``) and the
    default datetime resolution (``datetime64[us]`` ->
    ``datetime64[ns]``, timezone suffix preserved). Explicitly-requested
    extension dtypes (``string``, ``string[pyarrow]``, ...) pass through
    unchanged, as do all other dtypes.
    """
    label = str(dtype)
    if label == "str":
        return "object"
    m = _DATETIME_RES.match(label)
    if m:
        return f"datetime64[ns{m.group(2) or ''}]"
    return label


def utc_naive_datetimes(series: pd.Series) -> pd.Series:
    """Coerce to datetimes, drop what does not parse, and express tz-aware values as naive UTC.

    Snapshot datetime stats and the fitted resolution must read values in the same time
    frame, and two machines in different zones must not diverge, so both call this one rule.
    """
    stamps = pd.to_datetime(series, errors="coerce").dropna()
    if getattr(stamps.dtype, "tz", None) is not None:
        stamps = stamps.dt.tz_convert("UTC").dt.tz_localize(None)
    return stamps


# Each Arrow integer type maps to its own same-width, same-signedness nullable pandas dtype, so a
# null-bearing integer column stays exact (no float64 round trip) and keeps its width.
_ARROW_INT_TO_NULLABLE_DTYPE: Final[dict[pa.DataType, type[pd.api.extensions.ExtensionDtype]]] = {
    pa.int8(): pd.Int8Dtype,
    pa.int16(): pd.Int16Dtype,
    pa.int32(): pd.Int32Dtype,
    pa.int64(): pd.Int64Dtype,
    pa.uint8(): pd.UInt8Dtype,
    pa.uint16(): pd.UInt16Dtype,
    pa.uint32(): pd.UInt32Dtype,
    pa.uint64(): pd.UInt64Dtype,
}


def exact_int_types_mapper(arrow_type: pa.DataType) -> pd.api.extensions.ExtensionDtype | None:
    """`to_pandas(types_mapper=...)` hook: the nullable dtype for an Arrow integer type, else None."""
    dtype_cls = _ARROW_INT_TO_NULLABLE_DTYPE.get(arrow_type)
    return dtype_cls() if dtype_cls is not None else None


def table_to_pandas_exact_ints(
    table: pa.Table,
    columns: Iterable[str],
    *,
    types_mapper: Callable[[pa.DataType], pd.api.extensions.ExtensionDtype | None] = (
        exact_int_types_mapper
    ),
    on_unconvertible: Callable[[str], NoReturn] | None = None,
) -> pd.DataFrame:
    """`table.to_pandas()` with the named integer columns kept exact as nullable integer dtypes.

    Plain `to_pandas()` widens an integer column holding any null to float64, which rounds values
    beyond 2**53. Columns not named, or not integer-typed, keep pyarrow's default. A column that
    does not fit its own dtype calls `on_unconvertible(column)` (which must raise), or re-raises
    the pyarrow error when none is given.
    """
    df = table.to_pandas()
    failed = None
    for col in columns:
        if col not in table.column_names or not pa.types.is_integer(table.schema.field(col).type):
            continue
        try:
            series = table.column(col).to_pandas(types_mapper=types_mapper)
        except (pa.lib.ArrowInvalid, OverflowError):
            if on_unconvertible is None:
                raise
            failed = col
            break
        # `.array` carries no index, so the assignment is positional even when `df` has a
        # non-default index.
        df[col] = series.array
    if failed is not None and on_unconvertible is not None:
        on_unconvertible(failed)  # raised outside the except block, so no exception context
    return df
