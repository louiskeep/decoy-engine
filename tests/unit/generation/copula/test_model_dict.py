"""A6: `from_dict` is strict, never repairs, and its errors carry no value."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

from decoy_engine.generation.copula import GaussianCopulaModel, fit_gaussian_copula

from ._fixtures import numeric_frame, seed_bytes, snapshot

SENTINEL = "zz-planted-value-7731"
_FRAME = numeric_frame(1, n=400)
_MODEL = fit_gaussian_copula(_FRAME, snapshot(_FRAME), seed=seed_bytes(5))
_VALID = json.loads(json.dumps(_MODEL.to_dict()))

Mutation = Callable[[dict[str, Any]], None]


def _set(path: list[Any], value: Any) -> Mutation:
    def apply(d: dict[str, Any]) -> None:
        target: Any = d
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return apply


def _delete(path: list[Any]) -> Mutation:
    def apply(d: dict[str, Any]) -> None:
        target: Any = d
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return apply


def _bump_first_count(d: dict[str, Any]) -> None:
    d["columns"][0]["bin_counts"][0] += 1


def _decreasing_edges(d: dict[str, Any]) -> None:
    edges = d["columns"][0]["bin_edges"]
    edges[1], edges[2] = edges[2], edges[1]


def _not_definite(d: dict[str, Any]) -> None:
    d["correlation"] = [0.99, -0.99, 0.99]


CASES: dict[str, Mutation] = {
    "missing schema_version": _delete(["schema_version"]),
    "next major version": _set(["schema_version"], "decoy-gaussian-copula/v2"),
    "version with trailing newline": _set(["schema_version"], "decoy-gaussian-copula/v1\n"),
    "free-text version": _set(["schema_version"], SENTINEL),
    "extra top key": _set([SENTINEL], 1),
    "missing columns": _delete(["columns"]),
    "bool row_count": _set(["row_count"], True),
    "string row_count": _set(["row_count"], SENTINEL),
    "numeric-string count": _set(["columns", 0, "bin_counts", 0], "5"),
    "int name": _set(["columns", 0, "name"], 1),
    "unknown kind": _set(["columns", 0, "kind"], SENTINEL),
    "extra column key": _set(["columns", 0, SENTINEL], 1),
    "missing column key": _delete(["columns", 0, "dtype"]),
    "nan edge": _set(["columns", 0, "bin_edges", 0], float("nan")),
    "inf edge": _set(["columns", 0, "bin_edges", 0], float("inf")),
    "string edge": _set(["columns", 0, "bin_edges", 0], SENTINEL),
    "bool edge": _set(["columns", 0, "bin_edges", 0], True),
    "counts not summing": _bump_first_count,
    "decreasing edges": _decreasing_edges,
    "row counts disagree": _set(["columns", 0, "null_count"], 3),
    "row_count below floor": _set(["row_count"], 99),
    "correlation too short": _set(["correlation"], [0.1]),
    "correlation too long": _set(["correlation"], [0.1, 0.1, 0.1, 0.1]),
    "correlation above one": _set(["correlation", 0], 1.5),
    "correlation nan": _set(["correlation", 0], float("nan")),
    "correlation four decimals": _set(["correlation", 0], 0.1234),
    "correlation string": _set(["correlation", 0], SENTINEL),
    "not positive definite": _not_definite,
    "suppressed unknown reason": _set(["suppressed"], [{"variable": 0, "reason": SENTINEL}]),
    "suppressed out of range": _set(["suppressed"], [{"variable": 9, "reason": "pair_rows"}]),
    "suppressed bool index": _set(["suppressed"], [{"variable": True, "reason": "pair_rows"}]),
    "suppressed nonzero row": _set(["suppressed"], [{"variable": 0, "reason": "pair_rows"}]),
    "suppressed duplicate": _set(
        ["suppressed"],
        [{"variable": 0, "reason": "pair_rows"}, {"variable": 0, "reason": "pair_rows"}],
    ),
    "not a mapping": lambda d: d.clear(),
}


def test_valid_dict_loads() -> None:
    assert GaussianCopulaModel.from_dict(copy.deepcopy(_VALID)) == _MODEL


@pytest.mark.parametrize("name", sorted(CASES))
def test_corrupt_dict_is_refused_without_values(name: str) -> None:
    data = copy.deepcopy(_VALID)
    CASES[name](data)
    with pytest.raises((ValueError, TypeError)) as info:
        GaussianCopulaModel.from_dict(data)
    assert SENTINEL not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_suppressed_variable_with_zero_row_loads() -> None:
    data = copy.deepcopy(_VALID)
    data["correlation"] = [0.0, 0.0, 0.5]
    data["suppressed"] = [{"variable": 0, "reason": "rows_per_bin"}]
    model = GaussianCopulaModel.from_dict(data)
    assert model.suppressed[0].reason == "rows_per_bin"
