"""The hierarchy contract and the one place that applies a level to a column.

Every kind subclasses ``Hierarchy`` and supplies only ``height``, ``_covers``
and ``_labels``. ``Hierarchy.generalize`` owns everything shared: level
checks, level 0 as the identity, ``"*"`` for every row at the top, nulls kept
null below the top, the coverage refusal, value identity, and the pandas and
Arrow routes. Subclasses must not override it (a guard test checks).

Distinct values are labelled once and labels are taken back by code, so a
row's label depends only on its value and never on row order or chunking.

Errors never carry a cell value and are raised after any ``except`` block has
closed, so ``__cause__`` and ``__context__`` stay ``None``.
"""

from __future__ import annotations

import abc
import datetime
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, TypeAlias

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.anonymize._values import type_family

SUPPRESSED = "*"

Column: TypeAlias = pd.Series | pa.Array | pa.ChunkedArray

IntArray = npt.NDArray[np.int64]

# Failures pandas and Arrow raise for columns they cannot encode or convert
# (nested types, out-of-range temporal values).
_READ_ERRORS = (TypeError, ValueError, OverflowError, NotImplementedError, pa.ArrowException)

_UTC_NAMES = frozenset({"UTC", "Etc/UTC", "Etc/UCT", "UCT", "Zulu", "Etc/Zulu"})


class _Unhashable:
    """Stands in for every cell that cannot be hashed; such cells are uncovered."""

    __slots__ = ()


_UNHASHABLE = _Unhashable()


@dataclass(frozen=True)
class Distinct:
    """A column as codes into its distinct non-null values (``-1`` is null)."""

    codes: IntArray
    uniques: list[Any]
    route: str
    index: pd.Index | None
    name: Hashable | None

    @property
    def rows(self) -> int:
        return int(self.codes.shape[0])


class Hierarchy(abc.ABC):
    """A generalization hierarchy with levels ``0..height``.

    Level 0 is the value itself, levels ``1..height-1`` are the kind's string
    labels and level ``height`` is ``"*"`` for every row.
    """

    __slots__ = ()

    kind: ClassVar[str]

    @property
    @abc.abstractmethod
    def height(self) -> int:
        """Index of the top (fully suppressed) level."""

    @abc.abstractmethod
    def _covers(self, value: Any) -> bool:
        """Whether a distinct non-null value has a label at every level."""

    @abc.abstractmethod
    def _labels(self, values: Sequence[Any], level: int) -> list[str]:
        """Labels at ``level`` (``1..height-1``) for covered distinct values."""

    def generalize(self, column: Column, level: int) -> Column:
        """``column`` generalized to ``level``.

        pandas ``Series`` in gives a ``Series`` out with the same index and
        name; an Arrow ``Array`` or ``ChunkedArray`` gives a ``pa.Array``.
        Raises ``ValueError`` (with a row count, never a value) when a
        non-null value is not covered.
        """
        level = check_level(level, self.height)
        distinct = distinct_values(column)
        uncovered = uncovered_rows(self, distinct)
        if uncovered:
            raise ValueError(f"column has values the hierarchy does not cover: {uncovered} rows")
        if level == 0:
            return _identity(column)
        if level == self.height:
            out: npt.NDArray[np.object_] = np.full(distinct.rows, SUPPRESSED, dtype=object)
        else:
            out = take_labels(distinct, self._labels(distinct.uniques, level))
        return _wrap(out, distinct)


# ---------------------------------------------------------------- checks


def check_level(level: object, height: int) -> int:
    if type(level) is not int:
        raise TypeError("level must be an int (not bool, float or a numpy scalar)")
    if not 0 <= level <= height:
        raise ValueError(f"level must be between 0 and {height}")
    return level


def uncovered_rows(hierarchy: Hierarchy, distinct: Distinct) -> int:
    bad = [
        code
        for code, value in enumerate(distinct.uniques)
        if value is _UNHASHABLE or not hierarchy._covers(value)
    ]
    if not bad:
        return 0
    return int(np.isin(distinct.codes, np.asarray(bad, dtype=np.int64)).sum())


def take_labels(distinct: Distinct, labels: Sequence[str | None]) -> npt.NDArray[np.object_]:
    """Row labels from per-distinct labels; null rows stay ``None``."""
    table = np.empty(len(labels) + 1, dtype=object)
    table[: len(labels)] = list(labels)
    table[-1] = None
    return table[distinct.codes]


# ------------------------------------------------------------ reading


def distinct_values(column: object) -> Distinct:
    """Codes and distinct values of a column under the value-identity rule."""
    if isinstance(column, pd.Series):
        if isinstance(column.dtype, pd.ArrowDtype):
            # pandas keeps NaN as a value in Arrow-backed float columns; the
            # Arrow route nulls it, so both routes agree.
            codes, uniques = _arrow_codes(pa.array(column))
            return Distinct(codes, uniques, "pandas", column.index, column.name)
        if column.dtype == object:
            codes, uniques = _object_codes(column)
        elif column.dtype.kind == "f" and column.dtype.itemsize > 8:
            # pandas factorizes long doubles as doubles, merging distinct values.
            codes, uniques = _object_codes(column.astype(object))
        else:
            codes, uniques = _typed_codes(column)
        return Distinct(codes, uniques, "pandas", column.index, column.name)
    if isinstance(column, (pa.Array, pa.ChunkedArray)):
        codes, uniques = _arrow_codes(column)
        return Distinct(codes, uniques, "arrow", None, None)
    raise TypeError("column must be a pandas.Series, pyarrow.Array or pyarrow.ChunkedArray")


def _typed_codes(series: pd.Series) -> tuple[IntArray, list[Any]]:
    # One dtype holds one type family, so pandas' own factorize is exact here.
    failed = False
    try:
        codes, uniques = pd.factorize(series, use_na_sentinel=True)
        values = list(uniques.tolist())
    except _READ_ERRORS:
        failed = True
    if failed:
        raise TypeError("column values could not be read")
    return np.asarray(codes, dtype=np.int64), values


def _object_codes(series: pd.Series) -> tuple[IntArray, list[Any]]:
    cells = series.to_numpy(dtype=object, na_value=None)
    nulls = series.isna().to_numpy(dtype=bool)
    _check_one_zone(cells, nulls)
    codes = np.full(cells.shape[0], -1, dtype=np.int64)
    seen: dict[tuple[str, Any], int] = {}
    uniques: list[Any] = []
    unhashable = -1
    for row, cell in enumerate(cells):
        if nulls[row]:
            continue
        key = (type_family(cell), cell)
        try:
            code = seen.get(key)
        except TypeError:
            if unhashable < 0:
                unhashable = len(uniques)
                uniques.append(_UNHASHABLE)
            codes[row] = unhashable
            continue
        if code is None:
            code = len(uniques)
            seen[key] = code
            uniques.append(cell)
        codes[row] = code
    return codes, uniques


def _arrow_codes(column: pa.Array | pa.ChunkedArray) -> tuple[IntArray, list[Any]]:
    failed = False
    try:
        array = _combined(column)
        if pa.types.is_dictionary(array.type):
            array = array.dictionary_decode()
        if pa.types.is_floating(array.type):
            nan = pc.is_nan(array)  # type: ignore[attr-defined, unused-ignore]
            array = pc.if_else(nan, pa.scalar(None, array.type), array)  # type: ignore[attr-defined, unused-ignore]
        encoded = array.dictionary_encode()
        codes = pc.fill_null(encoded.indices, -1).to_numpy(zero_copy_only=False)
        uniques = _arrow_uniques(encoded.dictionary)
    except _READ_ERRORS:
        failed = True
    if failed:
        raise TypeError("column values could not be read")
    return np.asarray(codes, dtype=np.int64), list(uniques)


def _arrow_uniques(dictionary: pa.Array) -> list[Any]:
    kind = dictionary.type
    if pa.types.is_timestamp(kind) or pa.types.is_date(kind):
        # numpy datetime64, as pandas reads the same column, so both routes
        # label alike and a far-out value is uncovered rather than unreadable;
        # a tz-aware column is read as its local wall time.
        if pa.types.is_timestamp(kind) and kind.tz is not None:
            dictionary = pc.local_timestamp(dictionary)  # type: ignore[attr-defined, unused-ignore]
        return list(dictionary.to_numpy(zero_copy_only=False))
    return list(dictionary.to_pylist())


def _combined(column: pa.Array | pa.ChunkedArray) -> pa.Array:
    if not isinstance(column, pa.ChunkedArray):
        return column
    if pa.types.is_dictionary(column.type):
        # Chunks may carry different dictionaries; decode each before joining.
        value_type = column.type.value_type
        column = pa.chunked_array(
            [chunk.dictionary_decode() for chunk in column.chunks], type=value_type
        )
    if column.num_chunks == 0:
        return pa.array([], type=column.type)
    return column.combine_chunks()


# ------------------------------------------------------------ time zones


def zone_key(tz: datetime.tzinfo | None) -> str | None:
    """The name two tz-aware cells must share to count as one zone."""
    if tz is None:
        return None
    if isinstance(tz, datetime.timezone):
        offset = tz.utcoffset(None)
        return "UTC" if offset == datetime.timedelta(0) else f"offset {offset}"
    name = getattr(tz, "key", None) or getattr(tz, "zone", None)
    if isinstance(name, str):
        return "UTC" if name in _UTC_NAMES else name
    return f"{type(tz).__module__}.{type(tz).__qualname__} {tz}"


def _check_one_zone(cells: npt.NDArray[np.object_], nulls: npt.NDArray[np.bool_]) -> None:
    zones: set[str | None] = set()
    for row, cell in enumerate(cells):
        if nulls[row]:
            continue
        if isinstance(cell, datetime.datetime):
            zones.add(zone_key(cell.tzinfo))
        elif isinstance(cell, np.datetime64):
            zones.add(None)  # numpy datetimes carry no zone: naive
    if len(zones) > 1:
        raise TypeError(
            "column holds datetimes in mixed time zones (or naive mixed with aware); "
            "convert it to one zone first"
        )


# ------------------------------------------------------------ writing


def _identity(column: Column) -> Column:
    if isinstance(column, pd.Series):
        return column.copy()
    if isinstance(column, pa.ChunkedArray):
        # Kept as typed (dictionaries stay encoded), as for a plain Array.
        return column.combine_chunks()
    return column


def _wrap(out: npt.NDArray[np.object_], distinct: Distinct) -> Column:
    if distinct.route == "pandas":
        return pd.Series(out, index=distinct.index, name=distinct.name, dtype=object)
    return pa.array(out.tolist(), type=pa.string())
