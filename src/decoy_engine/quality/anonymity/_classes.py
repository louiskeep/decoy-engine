"""Shared input handling for the anonymity measures: one class identity, one null policy.

Every measure gets its equivalence classes from ``equivalence_classes`` and
its sensitive counts from ``sensitive_counts``, so k, l and t cannot
disagree on what a class is or on how nulls count. Grouping follows the
profiler's own mechanism (pandas ``factorize`` then ``groupby``), done on
integer codes so mixed object values and the several null spellings are
handled in one place.

Errors raised here never carry a cell value: the original exception (whose
message can quote one) is dropped by raising after the ``except`` block has
closed, so ``__cause__`` and ``__context__`` are both ``None``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.anonymize import type_family

IntArray = npt.NDArray[np.int64]

# Failures pandas and Arrow raise for cells that cannot be hashed, compared or
# converted (lists, dicts, Arrow list and struct columns, out-of-range values).
_CELL_ERRORS = (
    TypeError,
    ValueError,
    OverflowError,
    NotImplementedError,
    pa.ArrowException,
)


@dataclass(frozen=True)
class Classes:
    """Row-to-class mapping. ``ids`` are dense ``0..count-1``."""

    ids: IntArray
    sizes: IntArray
    rows_with_null_quasi_identifier: int

    @property
    def count(self) -> int:
        return int(self.sizes.shape[0])


@dataclass(frozen=True)
class SensitiveCounts:
    """Sparse (class, value) counts over non-null sensitive cells.

    ``pair_class``/``pair_value``/``pair_count`` are sorted by class then
    value code; ``class_totals`` is n_c per class, ``value_totals`` is the
    table count per value, ``value_count`` is m.
    """

    pair_class: IntArray
    pair_value: IntArray
    pair_count: IntArray
    class_totals: IntArray
    value_totals: IntArray
    value_count: int
    rows_with_null: int


# ---------------------------------------------------------------- validation


def check_columns(
    table: Any, quasi_identifiers: Sequence[str], sensitive: str | None
) -> tuple[str, ...]:
    """Validate names; return the columns to read (quasi-identifiers, then sensitive)."""
    if not isinstance(table, (pd.DataFrame, pa.Table)):
        raise TypeError("table must be a pandas.DataFrame or a pyarrow.Table")
    if isinstance(quasi_identifiers, (str, bytes)) or not isinstance(quasi_identifiers, Sequence):
        raise TypeError("quasi_identifiers must be a sequence of column names, not a string")
    names = tuple(quasi_identifiers)
    if not names:
        raise ValueError("quasi_identifiers must name at least one column")
    for name in names:
        if type(name) is not str:
            raise TypeError("quasi_identifiers must contain column names (str)")
    if len(set(names)) != len(names):
        raise ValueError("quasi_identifiers must not repeat a column")
    present = list(table.column_names if isinstance(table, pa.Table) else table.columns)
    wanted = names
    if sensitive is not None:
        if type(sensitive) is not str:
            raise TypeError("sensitive must be a column name (str)")
        if sensitive in names:
            raise ValueError(f"sensitive column {sensitive!r} is also a quasi-identifier")
        wanted = (*names, sensitive)
    for name in wanted:
        occurrences = present.count(name)
        if occurrences == 0:
            raise ValueError(f"column {name!r} is not in the table")
        if occurrences > 1:
            raise ValueError(f"column {name!r} appears more than once in the table")
    return wanted


def check_int_target(name: str, value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError(f"{name} must be an int (not bool, float or str)")
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _real(name: str, value: object) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{name} must be an int or float (not bool or str)")
    number = float(value)  # type: ignore[arg-type]
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def check_positive_real(name: str, value: object) -> float | None:
    if value is None:
        return None
    number = _real(name, value)
    if number <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return number


def check_unit_interval(name: str, value: object) -> float | None:
    if value is None:
        return None
    number = _real(name, value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return number


# --------------------------------------------------------------- reading


def _arrow_type_mapper(arrow_type: pa.DataType) -> Any:
    # Dictionaries keep the default conversion: it yields a pandas Categorical
    # that carries Arrow's ``ordered`` flag. Everything else stays Arrow-backed,
    # so int64 with nulls is not widened to float64 (2**53 + 1 survives).
    if pa.types.is_dictionary(arrow_type):
        return None
    return pd.ArrowDtype(arrow_type)


def _without_null_dictionary_entries(chunk: pa.DictionaryArray) -> pa.DictionaryArray:
    """Drop null and NaN dictionary entries, making rows that point at them null.

    pandas cannot hold a null category, and a null or NaN entry means a null
    cell under this module's null policy; the order of the remaining entries
    (and the ``ordered`` flag) is kept.
    """
    dictionary = chunk.dictionary
    bad = dictionary.is_null()
    if pa.types.is_floating(dictionary.type):
        nan = pc.fill_null(pc.is_nan(dictionary), False)  # type: ignore[attr-defined, unused-ignore]
        bad = pc.or_(bad, nan)  # type: ignore[attr-defined, unused-ignore]
    bad_np = bad.to_numpy(zero_copy_only=False)
    if not bad_np.any():
        return chunk
    remap = np.cumsum(~bad_np) - 1
    remap[bad_np] = -1
    valid = chunk.indices.is_valid().to_numpy(zero_copy_only=False)
    raw = pc.fill_null(chunk.indices, 0).to_numpy(zero_copy_only=False)
    mapped = remap[raw]
    null = ~valid | (mapped < 0)
    indices = pa.array(np.where(null, 0, mapped), type=chunk.indices.type, mask=null)
    kept = dictionary.filter(pc.invert(bad))  # type: ignore[attr-defined, unused-ignore]
    return pa.DictionaryArray.from_arrays(indices, kept, ordered=chunk.type.ordered)


def _subsequence_positions(part: pa.Array, whole: pa.Array) -> npt.NDArray[np.int64] | None:
    """Positions of ``part``'s entries in ``whole`` if they appear there in the
    same relative order, else None."""
    found = pc.index_in(part, value_set=whole)  # type: ignore[attr-defined, unused-ignore]
    if found.null_count:
        return None
    positions = found.to_numpy(zero_copy_only=False).astype(np.int64)
    if positions.size > 1 and not bool(np.all(np.diff(positions) > 0)):
        return None
    return positions


def _reindexed(
    chunk: pa.DictionaryArray, positions: IntArray, whole: pa.Array
) -> pa.DictionaryArray:
    valid = chunk.indices.is_valid().to_numpy(zero_copy_only=False)
    raw = pc.fill_null(chunk.indices, 0).to_numpy(zero_copy_only=False)
    mapped = positions[raw] if positions.size else np.zeros(raw.shape[0], dtype=np.int64)
    indices = pa.array(mapped, type=chunk.indices.type, mask=~valid)
    return pa.DictionaryArray.from_arrays(indices, whole, ordered=True)


def _reconcile_ordered_chunks(
    chunks: list[pa.DictionaryArray], field: pa.Field
) -> tuple[list[pa.DictionaryArray], pa.Field]:
    """One dictionary for all chunks of an ordered dictionary column.

    pandas merges differing chunk dictionaries in order of appearance and keeps
    ``ordered=True``, which invents an order (Parquet row groups often carry
    different dictionaries). When one chunk's dictionary holds every other
    chunk's entries in the same relative order, all chunks are re-indexed onto
    it. Otherwise no order is defined, and the column is marked unordered, so
    the ordered-distance path refuses it.

    Chunk dictionaries are deduplicated first, so the search is over distinct
    dictionaries only (one, for the usual repeated row-group dictionary).
    """
    distinct: dict[tuple[Any, ...], pa.Array] = {}
    keys = []
    for chunk in chunks:
        key = tuple(chunk.dictionary.to_pylist())
        distinct.setdefault(key, chunk.dictionary)
        keys.append(key)
    if len(distinct) == 1:
        return chunks, field
    for whole in sorted(distinct.values(), key=len, reverse=True):
        positions: dict[tuple[Any, ...], IntArray] = {}
        for key, dictionary in distinct.items():
            found = _subsequence_positions(dictionary, whole)
            if found is None:
                break
            positions[key] = found
        else:
            rebuilt = [
                _reindexed(chunk, positions[key], whole)
                for chunk, key in zip(chunks, keys, strict=True)
            ]
            return rebuilt, field
    unordered = pa.dictionary(field.type.index_type, field.type.value_type, ordered=False)
    relabelled = [
        pa.DictionaryArray.from_arrays(chunk.indices, chunk.dictionary, ordered=False)
        for chunk in chunks
    ]
    return relabelled, field.with_type(unordered)


def _clean_dictionaries(table: pa.Table, ordered: frozenset[str]) -> pa.Table:
    for position, field in enumerate(table.schema):
        if pa.types.is_dictionary(field.type):
            column = table.column(position)
            chunks = [_without_null_dictionary_entries(chunk) for chunk in column.chunks]
            if field.name in ordered and field.type.ordered and len(chunks) > 1:
                chunks, field = _reconcile_ordered_chunks(chunks, field)
            table = table.set_column(position, field, pa.chunked_array(chunks, type=field.type))
    return table


def read_columns(
    table: pd.DataFrame | pa.Table,
    columns: tuple[str, ...],
    ordered: frozenset[str] = frozenset(),
) -> pd.DataFrame:
    """The named columns as pandas, converted losslessly; value-free on failure.

    ``ordered`` names the columns whose category order is read (the ordered
    t-closeness sensitive column); only their chunked ordered dictionaries are
    reconciled, since every other use factorizes by value.
    """
    if isinstance(table, pd.DataFrame):
        return table.loc[:, list(columns)]
    failed = False
    try:
        selected = _clean_dictionaries(table.select(list(columns)), ordered)
        frame = selected.to_pandas(types_mapper=_arrow_type_mapper)
    except _CELL_ERRORS:
        failed = True
    if failed:
        raise TypeError(
            f"columns {list(columns)!r} could not be read from the Arrow table "
            "(unsupported or out-of-range values)"
        )
    return frame


# ------------------------------------------------------------------ codes


def null_mask(series: pd.Series) -> npt.NDArray[np.bool_]:
    """Null cells, with Arrow float NaN counted as null (Arrow keeps the two apart)."""
    mask = series.isna().to_numpy(dtype=bool)
    dtype = series.dtype
    if isinstance(dtype, pd.ArrowDtype) and pa.types.is_floating(dtype.pyarrow_dtype):
        nan = pc.fill_null(pc.is_nan(pa.array(series)), False)  # type: ignore[attr-defined, unused-ignore]
        mask = mask | nan.to_numpy(zero_copy_only=False)
    return mask


def _factorize(values: Any) -> IntArray:
    codes, _ = pd.factorize(values, use_na_sentinel=True)
    return np.asarray(codes, dtype=np.int64)


def _unreadable(column: str) -> TypeError:
    return TypeError(
        f"column {column!r} holds values that cannot be compared or hashed "
        "(for example lists, dicts or nested Arrow types)"
    )


def _with_null_code(nonnull_codes: IntArray, mask: npt.NDArray[np.bool_]) -> IntArray:
    full = np.full(mask.shape[0], -1, dtype=np.int64)
    full[~mask] = nonnull_codes
    return full


# ``infer_dtype`` results (a C-level scan) that guarantee one type family, so
# the per-cell family pass can be skipped. "date" is not here: it also covers
# a mix of dates and datetimes, which are two families.
_SINGLE_FAMILY_INFERRED = frozenset(
    {
        "string",
        "bytes",
        "integer",
        "floating",
        "boolean",
        "decimal",
        "complex",
        "datetime",
        "datetime64",
        "timedelta",
        "timedelta64",
        "time",
    }
)


def _plain_codes(values: pd.Series) -> IntArray:
    return _factorize(values)


def _family_codes(values: pd.Series) -> IntArray:
    codes = _factorize(values)
    if values.dtype != object or not codes.size:
        return codes
    if pd.api.types.infer_dtype(values, skipna=False) in _SINGLE_FAMILY_INFERRED:
        return codes
    families = _factorize(np.array([type_family(v) for v in values], dtype=object))
    return _factorize(codes * (int(families.max()) + 1) + families)


def _nonnull_codes(
    series: pd.Series, column: str, encode: Callable[[pd.Series], IntArray]
) -> tuple[IntArray, npt.NDArray[np.bool_]]:
    mask = null_mask(series)
    failed = False
    try:
        codes = encode(series[~mask])
    except _CELL_ERRORS:
        failed = True
    if failed:
        raise _unreadable(column)
    return codes, mask


def quasi_identifier_codes(
    series: pd.Series, column: str
) -> tuple[IntArray, npt.NDArray[np.bool_]]:
    """Dense codes with nulls as one extra code; object cells keyed by type family."""
    codes, mask = _nonnull_codes(series, column, _family_codes)
    full = _with_null_code(codes, mask)
    full[mask] = int(codes.max()) + 1 if codes.size else 0
    return full, mask


def sensitive_codes(series: pd.Series, column: str) -> IntArray:
    """Codes by plain equality, -1 for null. For l-diversity, where merging
    equal-comparing values of different types can only lower l."""
    codes, mask = _nonnull_codes(series, column, _plain_codes)
    return _with_null_code(codes, mask)


def sensitive_family_codes(series: pd.Series, column: str) -> IntArray:
    """Codes keyed by type family, -1 for null. For t-closeness, where merging
    ``1``, ``1.0`` and ``True`` could only lower the distance."""
    codes, mask = _nonnull_codes(series, column, _family_codes)
    return _with_null_code(codes, mask)


def _is_orderable(dtype: Any) -> bool:
    if isinstance(dtype, pd.CategoricalDtype):
        return bool(dtype.ordered)
    if isinstance(dtype, pd.ArrowDtype):
        t = dtype.pyarrow_dtype
        return bool(
            pa.types.is_integer(t)
            or pa.types.is_floating(t)
            or pa.types.is_decimal(t)
            or pa.types.is_boolean(t)
            or pa.types.is_temporal(t)
        )
    if isinstance(dtype, pd.DatetimeTZDtype):
        return True
    if isinstance(dtype, np.dtype):
        return dtype.kind in "biufmM"
    return bool(pd.api.types.is_numeric_dtype(dtype) or pd.api.types.is_bool_dtype(dtype))


def ordered_sensitive_codes(series: pd.Series, column: str) -> IntArray:
    """Codes equal to each value's rank among the observed values; -1 for null."""
    if not _is_orderable(series.dtype):
        raise TypeError(
            f"ordered distance needs column {column!r} to be numeric, decimal, temporal "
            "or an ordered categorical (an Arrow ordered dictionary whose chunks disagree "
            "on the order of their entries has no defined order)"
        )
    mask = null_mask(series)
    if isinstance(series.dtype, pd.CategoricalDtype):
        cat_codes = series.cat.codes.to_numpy(dtype=np.int64)[~mask]
        observed = np.unique(cat_codes)
        return _with_null_code(np.searchsorted(observed, cat_codes).astype(np.int64), mask)
    codes, uniques = pd.factorize(series[~mask], use_na_sentinel=True)
    rank = np.empty(len(uniques), dtype=np.int64)
    rank[np.asarray(uniques.argsort())] = np.arange(len(uniques), dtype=np.int64)
    return _with_null_code(rank[np.asarray(codes, dtype=np.int64)], mask)


# --------------------------------------------------------- classes, counts


def equivalence_classes(frame: pd.DataFrame, quasi_identifiers: tuple[str, ...]) -> Classes:
    """The one place class identity is decided (null policy and value identity)."""
    n = len(frame)
    if n == 0:
        empty = np.zeros(0, dtype=np.int64)
        return Classes(ids=empty, sizes=empty, rows_with_null_quasi_identifier=0)
    code_columns = {}
    any_null = np.zeros(n, dtype=bool)
    for position, name in enumerate(quasi_identifiers):
        codes, mask = quasi_identifier_codes(frame.iloc[:, position], name)
        code_columns[position] = codes
        any_null |= mask
    codes_frame = pd.DataFrame(code_columns)
    ids = codes_frame.groupby(list(code_columns), sort=False).ngroup().to_numpy(dtype=np.int64)
    sizes = np.bincount(ids).astype(np.int64)
    return Classes(ids=ids, sizes=sizes, rows_with_null_quasi_identifier=int(any_null.sum()))


def sensitive_counts(classes: Classes, codes: IntArray) -> SensitiveCounts:
    """Sparse counts of non-null sensitive codes per class."""
    valid = codes >= 0
    value_count = int(codes.max()) + 1 if valid.any() else 0
    cls = classes.ids[valid]
    val = codes[valid]
    keys = cls * max(value_count, 1) + val
    unique_keys, pair_count = np.unique(keys, return_counts=True)
    divisor = max(value_count, 1)
    return SensitiveCounts(
        pair_class=(unique_keys // divisor).astype(np.int64),
        pair_value=(unique_keys % divisor).astype(np.int64),
        pair_count=pair_count.astype(np.int64),
        class_totals=np.bincount(cls, minlength=classes.count).astype(np.int64),
        value_totals=np.bincount(val, minlength=value_count).astype(np.int64),
        value_count=value_count,
        rows_with_null=int((~valid).sum()),
    )


def rows_in(classes: Classes, selected: npt.NDArray[np.bool_]) -> int:
    return int(classes.sizes[selected].sum())
