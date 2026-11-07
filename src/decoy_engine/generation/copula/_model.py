"""The fitted Gaussian copula model: its fields, its one validator, and its JSON form.

A model is the snapshot's own per-column marginal fields (the `distribution-snapshot/v1` entry of
each column, copied unchanged) plus the released latent correlation matrix and the list of latent
variables whose correlations were suppressed by the disclosure floors (`_floors.py`). It carries
no other statistic of the data. It is still a sensitive artifact: bin edges end at real minima and
maxima, and categorical marginals hold real labels, exactly as the snapshot does.

Every model, whether built by `fit_gaussian_copula` or read by `from_dict`, passes the same
validator in `__post_init__`; nothing is repaired on read. Messages name a field path and a rule,
never a value.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
import numpy.typing as npt

from decoy_engine.generation.copula import _floors
from decoy_engine.generation.copula._nearest_corr import min_eigenvalue

COPULA_MODEL_SCHEMA_VERSION = "decoy-gaussian-copula/v1"

KIND_NUMERIC = "numeric"
KIND_CATEGORICAL = "categorical"
KIND_EMPTY = "empty"
KINDS = (KIND_NUMERIC, KIND_CATEGORICAL, KIND_EMPTY)

ROLE_VALUE = "value"
ROLE_NULL = "null"
ROLE_STATE = "state"

_TOP_KEYS = frozenset({"schema_version", "row_count", "columns", "correlation", "suppressed"})
_COMMON_COLUMN_KEYS = ("name", "kind", "dtype", "null_count", "non_null_count")
_KIND_KEYS = {
    KIND_NUMERIC: ("bin_edges", "bin_counts"),
    KIND_CATEGORICAL: ("top_values", "other_count"),
    KIND_EMPTY: (),
}


@dataclass(frozen=True)
class ColumnMarginal:
    """One column's snapshot marginal. Only the fields of its `kind` are populated."""

    name: str
    kind: str
    dtype: str
    null_count: int
    non_null_count: int
    bin_edges: tuple[float, ...] = ()
    bin_counts: tuple[int, ...] = ()
    top_values: tuple[tuple[str, int], ...] = ()
    other_count: int = 0

    def __repr__(self) -> str:
        return f"ColumnMarginal(name={self.name!r}, kind={self.kind!r})"


@dataclass(frozen=True)
class LatentVariable:
    """A dimension of the copula: a column's value, its null indicator, or its categorical state."""

    column: int
    role: str


@dataclass(frozen=True)
class Suppression:
    variable: int
    reason: str


def latent_layout(columns: tuple[ColumnMarginal, ...]) -> tuple[LatentVariable, ...]:
    """The latent variable order, derived from the columns (never stored)."""
    out: list[LatentVariable] = []
    for index, column in enumerate(columns):
        if column.kind == KIND_NUMERIC:
            out.append(LatentVariable(index, ROLE_VALUE))
            if column.null_count > 0:
                out.append(LatentVariable(index, ROLE_NULL))
        elif column.kind == KIND_CATEGORICAL:
            out.append(LatentVariable(index, ROLE_STATE))
    return tuple(out)


def lower_triangle(matrix: npt.NDArray[np.float64]) -> tuple[float, ...]:
    rows, cols = np.tril_indices(matrix.shape[0], k=-1)
    return tuple(float(v) for v in matrix[rows, cols])


def _matrix_from_triangle(values: tuple[float, ...], dimension: int) -> npt.NDArray[np.float64]:
    matrix = np.eye(dimension)
    rows, cols = np.tril_indices(dimension, k=-1)
    matrix[rows, cols] = values
    matrix[cols, rows] = values
    return matrix


def _type_error(path: str, expected: str) -> TypeError:
    return TypeError(f"copula model: {path} must be {expected}.")


def _value_error(path: str, rule: str) -> ValueError:
    return ValueError(f"copula model: {path} {rule}.")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_count(value: object, path: str) -> None:
    if not _is_int(value):
        raise _type_error(path, "an integer")
    if value < 0:  # type: ignore[operator]
        raise _value_error(path, "must not be negative")


def _check_finite(value: object, path: str) -> None:
    if not _is_number(value):
        raise _type_error(path, "a number")
    if not math.isfinite(value):  # type: ignore[arg-type]
        raise _value_error(path, "must be finite")


def _validate_numeric(column: ColumnMarginal, path: str) -> None:
    edges, counts = column.bin_edges, column.bin_counts
    if column.non_null_count == 0:
        raise _value_error(f"{path}.non_null_count", "must be positive for a numeric column")
    if len(edges) < 2 or len(counts) != len(edges) - 1:
        raise _value_error(f"{path}.bin_edges", "must have one more entry than bin_counts")
    for i, edge in enumerate(edges):
        _check_finite(edge, f"{path}.bin_edges[{i}]")
    if any(b < a for a, b in itertools.pairwise(edges)):
        raise _value_error(f"{path}.bin_edges", "must be non-decreasing")
    for i, count in enumerate(counts):
        _check_count(count, f"{path}.bin_counts[{i}]")
    if sum(counts) != column.non_null_count:
        raise _value_error(f"{path}.bin_counts", "must sum to non_null_count")
    if column.top_values or column.other_count:
        raise _value_error(path, "carries categorical fields on a numeric column")


def _validate_categorical(column: ColumnMarginal, path: str) -> None:
    if column.non_null_count == 0 or not column.top_values:
        raise _value_error(f"{path}.top_values", "must not be empty for a categorical column")
    labels: set[str] = set()
    for i, entry in enumerate(column.top_values):
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise _type_error(f"{path}.top_values[{i}]", "a (value, count) pair")
        label, count = entry
        if not isinstance(label, str):
            raise _type_error(f"{path}.top_values[{i}].value", "a string")
        _check_count(count, f"{path}.top_values[{i}].count")
        if count == 0:
            raise _value_error(f"{path}.top_values[{i}].count", "must be positive")
        if label in labels:
            raise _value_error(f"{path}.top_values[{i}].value", "duplicates an earlier label")
        labels.add(label)
    canonical = sorted(column.top_values, key=lambda kv: (-kv[1], kv[0]))
    if list(column.top_values) != canonical:
        raise _value_error(f"{path}.top_values", "must be ordered by count, then label")
    _check_count(column.other_count, f"{path}.other_count")
    if sum(c for _, c in column.top_values) + column.other_count != column.non_null_count:
        raise _value_error(
            f"{path}.top_values", "counts plus other_count must equal non_null_count"
        )
    if column.bin_edges or column.bin_counts:
        raise _value_error(path, "carries numeric fields on a categorical column")


def _validate_column(column: object, index: int, row_count: int) -> None:
    path = f"columns[{index}]"
    if not isinstance(column, ColumnMarginal):
        raise _type_error(path, "a ColumnMarginal")
    for field in ("name", "dtype"):
        if not isinstance(getattr(column, field), str):
            raise _type_error(f"{path}.{field}", "a string")
    if column.kind not in KINDS:
        raise _value_error(f"{path}.kind", "is not a known kind")
    _check_count(column.null_count, f"{path}.null_count")
    _check_count(column.non_null_count, f"{path}.non_null_count")
    if column.null_count + column.non_null_count != row_count:
        raise _value_error(path, "null_count plus non_null_count must equal row_count")
    if column.kind == KIND_NUMERIC:
        _validate_numeric(column, path)
    elif column.kind == KIND_CATEGORICAL:
        _validate_categorical(column, path)
    elif column.non_null_count != 0 or column.bin_edges or column.top_values:
        raise _value_error(path, "an empty column must have no values and no stats")


def _validate_correlation(model: GaussianCopulaModel, dimension: int) -> None:
    if dimension > _floors.MAX_LATENT:
        raise _value_error("columns", "describe more latent variables than the maximum")
    pairs = dimension * (dimension - 1) // 2
    if pairs > _floors.max_latent_pairs(model.row_count):
        raise _value_error("columns", "describe more latent pairs than the row budget allows")
    if not isinstance(model.correlation, tuple):
        raise _type_error("correlation", "a sequence of numbers")
    if len(model.correlation) != pairs:
        raise _value_error("correlation", "has the wrong length for the latent dimension")
    for i, value in enumerate(model.correlation):
        _check_finite(value, f"correlation[{i}]")
        if abs(value) > 1.0:
            raise _value_error(f"correlation[{i}]", "must lie in [-1, 1]")
        if round(value, _floors.RELEASE_DECIMALS) != value:
            raise _value_error(f"correlation[{i}]", "has more than the released decimals")
    matrix = _matrix_from_triangle(model.correlation, dimension)
    seen: set[int] = set()
    previous = -1
    for i, entry in enumerate(model.suppressed):
        path = f"suppressed[{i}]"
        if not isinstance(entry, Suppression):
            raise _type_error(path, "a Suppression")
        if not _is_int(entry.variable):
            raise _type_error(f"{path}.variable", "an integer")
        if not 0 <= entry.variable < dimension:
            raise _value_error(f"{path}.variable", "is not a latent variable index")
        if entry.variable in seen or entry.variable < previous:
            raise _value_error(f"{path}.variable", "must be unique and in ascending order")
        if entry.reason not in _floors.SUPPRESSION_TOKENS:
            raise _value_error(f"{path}.reason", "is not a known suppression reason")
        row = matrix[entry.variable].copy()
        row[entry.variable] = 0.0
        if np.any(row != 0.0):
            raise _value_error(path, "names a variable with a nonzero correlation")
        seen.add(entry.variable)
        previous = entry.variable
    if min_eigenvalue(matrix) < _floors.MIN_EIGENVALUE:
        raise _value_error("correlation", "is not positive definite with the required margin")


@dataclass(frozen=True, repr=False)
class GaussianCopulaModel:
    """A validated, immutable copula model. `repr` names columns and kinds only."""

    row_count: int
    columns: tuple[ColumnMarginal, ...]
    correlation: tuple[float, ...]
    suppressed: tuple[Suppression, ...] = ()

    def __post_init__(self) -> None:
        _check_count(self.row_count, "row_count")
        if self.row_count < _floors.MIN_FIT_ROWS:
            raise _value_error("row_count", "is below the minimum rows for a copula model")
        if not isinstance(self.columns, tuple) or not self.columns:
            raise _type_error("columns", "a non-empty tuple of ColumnMarginal")
        names: set[str] = set()
        for index, column in enumerate(self.columns):
            _validate_column(column, index, self.row_count)
            if column.name in names:
                raise _value_error(f"columns[{index}].name", "duplicates an earlier column")
            names.add(column.name)
        if not isinstance(self.suppressed, tuple):
            raise _type_error("suppressed", "a tuple of Suppression")
        _validate_correlation(self, len(self.latent))

    @property
    def latent(self) -> tuple[LatentVariable, ...]:
        return latent_layout(self.columns)

    def correlation_matrix(self) -> npt.NDArray[np.float64]:
        return _matrix_from_triangle(self.correlation, len(self.latent))

    def __repr__(self) -> str:
        cols = ", ".join(f"{c.name}:{c.kind}" for c in self.columns)
        return (
            f"GaussianCopulaModel(rows={self.row_count}, latent={len(self.latent)}, "
            f"suppressed={len(self.suppressed)}, columns=[{cols}])"
        )

    def to_dict(self) -> dict[str, Any]:
        columns: list[dict[str, Any]] = []
        for column in self.columns:
            entry: dict[str, Any] = {key: getattr(column, key) for key in _COMMON_COLUMN_KEYS}
            if column.kind == KIND_NUMERIC:
                entry["bin_edges"] = list(column.bin_edges)
                entry["bin_counts"] = list(column.bin_counts)
            elif column.kind == KIND_CATEGORICAL:
                entry["top_values"] = [{"value": v, "count": c} for v, c in column.top_values]
                entry["other_count"] = column.other_count
            columns.append(entry)
        return {
            "schema_version": COPULA_MODEL_SCHEMA_VERSION,
            "row_count": self.row_count,
            "columns": columns,
            "correlation": list(self.correlation),
            "suppressed": [{"variable": s.variable, "reason": s.reason} for s in self.suppressed],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GaussianCopulaModel:
        """Strict reader: exact schema version, exact keys, strict JSON types; never repairs."""
        if not isinstance(data, Mapping):
            raise _type_error("model", "a mapping")
        _exact_keys(data, _TOP_KEYS, "model")
        version = data["schema_version"]
        if not isinstance(version, str) or version != COPULA_MODEL_SCHEMA_VERSION:
            raise _value_error("schema_version", "is not a supported copula model version")
        raw_columns = _list(data["columns"], "columns")
        columns = tuple(_column_from_dict(entry, i) for i, entry in enumerate(raw_columns))
        correlation = tuple(
            _number(v, f"correlation[{i}]")
            for i, v in enumerate(_list(data["correlation"], "correlation"))
        )
        suppressed = tuple(
            _suppression_from_dict(entry, i)
            for i, entry in enumerate(_list(data["suppressed"], "suppressed"))
        )
        return cls(_int(data["row_count"], "row_count"), columns, correlation, suppressed)


def _exact_keys(
    data: Mapping[str, Any], expected: frozenset[str] | tuple[str, ...], path: str
) -> None:
    keys = set(data)
    if keys != set(expected):
        missing = sorted(set(expected) - keys)
        extra = len(keys - set(expected))
        raise _value_error(
            path, f"must have exactly its schema keys (missing {missing}, {extra} unknown)"
        )


def _list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _type_error(path, "a list")
    return value


def _str(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise _type_error(path, "a string")
    return value


def _int(value: object, path: str) -> int:
    if not _is_int(value):
        raise _type_error(path, "an integer")
    return cast(int, value)


def _number(value: object, path: str) -> float:
    if not _is_number(value):
        raise _type_error(path, "a number")
    return float(cast(float, value))


def _column_from_dict(entry: object, index: int) -> ColumnMarginal:
    path = f"columns[{index}]"
    if not isinstance(entry, Mapping):
        raise _type_error(path, "a mapping")
    kind = entry.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise _value_error(f"{path}.kind", "is not a known kind")
    _exact_keys(entry, _COMMON_COLUMN_KEYS + _KIND_KEYS[kind], path)
    common: dict[str, Any] = {
        "name": _str(entry["name"], f"{path}.name"),
        "kind": kind,
        "dtype": _str(entry["dtype"], f"{path}.dtype"),
        "null_count": _int(entry["null_count"], f"{path}.null_count"),
        "non_null_count": _int(entry["non_null_count"], f"{path}.non_null_count"),
    }
    if kind == KIND_NUMERIC:
        edges = _list(entry["bin_edges"], f"{path}.bin_edges")
        counts = _list(entry["bin_counts"], f"{path}.bin_counts")
        return ColumnMarginal(
            **common,
            bin_edges=tuple(_number(v, f"{path}.bin_edges[{i}]") for i, v in enumerate(edges)),
            bin_counts=tuple(_int(v, f"{path}.bin_counts[{i}]") for i, v in enumerate(counts)),
        )
    if kind == KIND_CATEGORICAL:
        top: list[tuple[str, int]] = []
        for i, item in enumerate(_list(entry["top_values"], f"{path}.top_values")):
            item_path = f"{path}.top_values[{i}]"
            if not isinstance(item, Mapping):
                raise _type_error(item_path, "a mapping")
            _exact_keys(item, ("value", "count"), item_path)
            top.append(
                (
                    _str(item["value"], f"{item_path}.value"),
                    _int(item["count"], f"{item_path}.count"),
                )
            )
        return ColumnMarginal(
            **common,
            top_values=tuple(top),
            other_count=_int(entry["other_count"], f"{path}.other_count"),
        )
    return ColumnMarginal(**common)


def _suppression_from_dict(entry: object, index: int) -> Suppression:
    path = f"suppressed[{index}]"
    if not isinstance(entry, Mapping):
        raise _type_error(path, "a mapping")
    _exact_keys(entry, ("variable", "reason"), path)
    return Suppression(
        _int(entry["variable"], f"{path}.variable"), _str(entry["reason"], f"{path}.reason")
    )
