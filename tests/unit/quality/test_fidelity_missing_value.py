"""GEN-FIT-SHAPE E3: null-rate drift in `compute_fidelity` and the shared null fraction.

AT-10 of docs/plans (gen-fit-shape): the missing-value factor multiplies into the column
similarity only when null rates differ, so every score on matching rates stays bitwise
what it was.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from decoy_engine.quality import snapshot_null_fraction
from decoy_engine.quality.fidelity import compute_fidelity


def _cat(null_count: int, non_null_count: int, split: tuple[int, int] = (6, 4)) -> dict[str, Any]:
    a = non_null_count * split[0] // sum(split)
    return {
        "dtype": "object",
        "kind": "categorical",
        "null_count": null_count,
        "non_null_count": non_null_count,
        "distinct_count": 2,
        "stats": {
            "top_values": [
                {"value": "a", "count": a},
                {"value": "b", "count": non_null_count - a},
            ],
            "other_count": 0,
        },
    }


def _num(null_count: int, non_null_count: int, median: float = 50.0) -> dict[str, Any]:
    return {
        "dtype": "float64",
        "kind": "numeric",
        "null_count": null_count,
        "non_null_count": non_null_count,
        "distinct_count": non_null_count,
        "stats": {
            "min": 0.0,
            "max": 100.0,
            "mean": 50.0,
            "std": 25.0,
            "quantiles": {
                "p05": 5.0,
                "p25": 25.0,
                "p50": median,
                "p75": 75.0,
                "p95": 95.0,
            },
            "bin_edges": [],
            "bin_counts": [],
        },
    }


def _empty(null_count: int) -> dict[str, Any]:
    return {
        "dtype": "object",
        "kind": "empty",
        "null_count": null_count,
        "non_null_count": 0,
        "distinct_count": 0,
        "stats": {},
    }


def _snap(columns: dict[str, dict[str, Any]], dp: bool = False) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "schema_version": "distribution-snapshot/v1",
        "row_count": 1000,
        "columns": columns,
        "joints": [],
    }
    if dp:
        snap["dp"] = {"column_schema": {}}
    return snap


def _entry(fid: dict[str, Any], column: str) -> dict[str, Any]:
    return next(c for c in fid["marginal"]["columns"] if c["column"] == column)


# ── snapshot_null_fraction ─────────────────────────────────────────────────


def test_null_fraction_is_public_and_exact() -> None:
    assert snapshot_null_fraction({"null_count": 138, "non_null_count": 862}) == 0.138
    assert snapshot_null_fraction({"null_count": 0, "non_null_count": 5}) == 0.0
    assert snapshot_null_fraction({"null_count": 5, "non_null_count": 0}) == 1.0


@pytest.mark.parametrize(
    "col",
    [
        {"null_count": None, "non_null_count": 5},
        {"null_count": "x", "non_null_count": 5},
        {"null_count": True, "non_null_count": 5},
        {"null_count": 1, "non_null_count": False},
        {"null_count": -1, "non_null_count": 5},
        {"null_count": 3, "non_null_count": -3},
        {"null_count": 1.5, "non_null_count": 5},
    ],
)
def test_null_fraction_malformed_counts_are_none(col: dict[str, Any]) -> None:
    assert snapshot_null_fraction(col) is None


def test_null_fraction_zero_rows_is_none() -> None:
    assert snapshot_null_fraction({"null_count": 0, "non_null_count": 0}) is None
    assert snapshot_null_fraction({}) is None


# ── AT-10 ──────────────────────────────────────────────────────────────────


def test_dropped_nulls_lower_similarity_by_the_missing_factor() -> None:
    src = _snap({"smoker": _cat(138, 862)})
    out = _snap({"smoker": _cat(0, 1000)})
    col = _entry(compute_fidelity(src, out), "smoker")
    assert col["missing_value_similarity"] == pytest.approx(0.862)
    assert col["value_similarity"] is not None
    assert col["similarity"] == pytest.approx(round(col["value_similarity"] * 0.862, 6))
    assert col["similarity"] < col["value_similarity"]
    assert col["comparable"] is True


def test_matching_null_rates_keep_the_exact_value_score() -> None:
    src = _snap({"a": _num(100, 900), "b": _cat(250, 750)})
    out = _snap({"a": _num(100, 900, median=40.0), "b": _cat(250, 750, split=(3, 7))})
    fid = compute_fidelity(src, out)
    for name in ("a", "b"):
        col = _entry(fid, name)
        assert col["missing_value_similarity"] == 1.0
        assert col["similarity"] == col["value_similarity"]
    # Same rate on a different row count is still a match.
    out2 = _snap({"a": _num(10, 90, median=40.0)})
    col = _entry(compute_fidelity(src, out2), "a")
    assert col["similarity"] == col["value_similarity"]


def test_literal_product_cell() -> None:
    # Value similarity exactly 0.8: quantile RMSE 10 over the IQR of 50 is one fifth.
    src = _snap({"x": _num(0, 1000)})
    out = _snap({"x": _num(200, 800)})
    out["columns"]["x"]["stats"]["quantiles"] = {k: v + 10.0 for k, v in _QUANTS.items()}
    col = _entry(compute_fidelity(src, out), "x")
    assert col["value_similarity"] == 0.8
    assert col["missing_value_similarity"] == 0.8
    assert col["similarity"] == 0.64


_QUANTS = {"p05": 5.0, "p25": 25.0, "p50": 50.0, "p75": 75.0, "p95": 95.0}


def test_value_times_missing_cell() -> None:
    # Value similarity 0.9: median 50 vs 40 over range 100 is RMSE-driven; build it
    # from the real comparator, then check the product rule against its own output.
    src = _snap({"x": _num(0, 1000)})
    base = _entry(compute_fidelity(src, _snap({"x": _num(0, 1000, median=40.0)})), "x")
    out = _snap({"x": _num(200, 800, median=40.0)})
    col = _entry(compute_fidelity(src, out), "x")
    assert col["value_similarity"] == base["value_similarity"]
    assert col["missing_value_similarity"] == pytest.approx(0.8)
    assert col["similarity"] == pytest.approx(round(base["value_similarity"] * 0.8, 6))


def test_marginal_score_uses_the_factored_similarity() -> None:
    src = _snap({"s": _cat(138, 862)})
    out = _snap({"s": _cat(0, 1000)})
    fid = compute_fidelity(src, out)
    assert fid["marginal"]["score"] == _entry(fid, "s")["similarity"]


def test_empty_versus_filled_stays_non_comparable_but_reports_missing() -> None:
    src = _snap({"e": _empty(1000), "n": _num(0, 1000)})
    out = _snap({"e": _cat(0, 1000), "n": _num(0, 1000)})
    col = _entry(compute_fidelity(src, out), "e")
    assert col["comparable"] is False
    assert col["similarity"] is None
    assert col["method"] == "kind_mismatch"
    assert col["missing_value_similarity"] == 0.0


def test_empty_versus_empty_reports_full_missing_similarity() -> None:
    src = _snap({"e": _empty(1000)})
    col = _entry(compute_fidelity(src, src), "e")
    assert col["method"] == "empty"
    assert col["comparable"] is False
    assert col["missing_value_similarity"] == 1.0


def test_whole_column_nulling_does_not_enter_the_aggregate() -> None:
    src = _snap({"n": _num(0, 1000), "k": _cat(0, 1000)})
    out = _snap({"n": _empty(1000), "k": _cat(0, 1000)})
    fid = compute_fidelity(src, out)
    assert _entry(fid, "n")["comparable"] is False
    assert fid["marginal"]["score"] == _entry(fid, "k")["similarity"] == 1.0


def test_dp_snapshot_reports_but_does_not_multiply() -> None:
    src = _snap({"x": _num(138, 862)}, dp=True)
    out = _snap({"x": _num(0, 1000)})
    col = _entry(compute_fidelity(src, out), "x")
    assert col["missing_value_similarity"] == pytest.approx(0.862)
    assert col["similarity"] == col["value_similarity"]
    # Either side being DP is enough.
    out_dp = _snap({"x": _num(0, 1000)}, dp=True)
    plain = _snap({"x": _num(138, 862)})
    col3 = _entry(compute_fidelity(plain, out_dp), "x")
    assert col3["similarity"] == col3["value_similarity"]


@pytest.mark.parametrize("side", ["source", "output"])
def test_zero_row_side_reports_none_without_dividing(side: str) -> None:
    zero = _num(0, 0)
    full = _num(10, 90)
    src, out = (zero, full) if side == "source" else (full, zero)
    col = _entry(compute_fidelity(_snap({"x": src}), _snap({"x": out})), "x")
    assert col["missing_value_similarity"] is None
    assert col["similarity"] == col["value_similarity"]
    json.dumps(col)


def test_symmetric_in_source_and_output() -> None:
    a = _snap({"s": _cat(138, 862)})
    b = _snap({"s": _cat(0, 1000)})
    assert (
        _entry(compute_fidelity(a, b), "s")["similarity"]
        == _entry(compute_fidelity(b, a), "s")["similarity"]
    )


def test_inputs_are_not_mutated() -> None:
    src = _snap({"s": _cat(138, 862)})
    out = _snap({"s": _cat(0, 1000)})
    before = copy.deepcopy((src, out))
    compute_fidelity(src, out)
    assert (src, out) == before
