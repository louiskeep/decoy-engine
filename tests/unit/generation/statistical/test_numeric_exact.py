"""Exact-value sampling of fitted number columns with few levels (GEN-NUM-SHAPE, row 24u)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.statistical import StatisticalSpecError, load_spec
from decoy_engine.profiling import distribution_snapshot_document
from tests.unit.generation.statistical._cf_support import draw, fit, similarity

_PRICES = [4.99, 9.99, 19.99, 49.99]
_SHARES = [0.4, 0.3, 0.2, 0.1]


def _prices(size: int = 20_000) -> np.ndarray:
    return np.random.default_rng(503).choice(_PRICES, p=_SHARES, size=size)


def _fit(values: Any, **kw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    snap = fit(pd.DataFrame({"x": values}), cell_format=False, **kw)
    return snap, snap["columns"]["x"]["stats"]


def _check_prices(snap: dict[str, Any], src: np.ndarray) -> None:
    out = draw(snap)
    assert set(out) <= set(_PRICES)
    for price in _PRICES:
        assert abs(out.count(price) / len(out) - float((src == price).mean())) <= 0.015
    assert similarity(snap, out) >= 0.99


def test_four_prices_stay_four_prices() -> None:
    src = _prices()
    snap, stats = _fit(src)
    assert "quantile_grid" not in stats
    _check_prices(snap, src)
    document = json.loads(
        json.dumps(distribution_snapshot_document(pd.DataFrame({"x": src}), include_values=True))
    )
    assert document["columns"]["x"]["stats"]["exact_values"] is not None
    _check_prices(document, src)


def test_a_rare_value_is_never_emitted() -> None:
    src = np.append(_prices(19_999), 999.99)
    snap, stats = _fit(src)
    assert stats["exact_values"]["values"] == _PRICES
    assert stats["exact_values"]["dropped_count"] == 1
    assert 999.99 not in draw(snap)


def test_exact_typing_follows_445() -> None:
    ints, _ = _fit(np.tile(np.array([1, 2, 3], dtype=np.int64), 1_000))
    assert all(type(v) is int for v in draw(ints, 2_000))
    floats, _ = _fit(np.tile([0.5, 1.5], 1_000))
    assert all(type(v) is float for v in draw(floats, 2_000))


def test_restored_exact_values_on_a_redacted_column_are_refused() -> None:
    raw = json.loads(
        json.dumps(
            distribution_snapshot_document(pd.DataFrame({"x": _prices()}), include_values=True)
        )
    )
    redacted = json.loads(json.dumps(raw))
    redacted["value_policy"] = "flagged-redacted/v1"
    redacted["redacted_columns"] = ["x"]
    redacted["columns"]["x"]["stats"]["exact_values"] = None
    load_spec({"name": "x"}, snapshot=redacted)
    redacted["columns"]["x"]["stats"]["exact_values"] = raw["columns"]["x"]["stats"]["exact_values"]
    with pytest.raises(StatisticalSpecError) as caught:
        load_spec({"name": "x"}, snapshot=redacted)
    assert caught.value.code == "statistical_snapshot_values_redacted"


def _bad_blocks() -> list[tuple[str, Any]]:
    good: dict[str, Any] = {"values": [4.99, 9.99], "counts": [30, 20], "dropped_count": 0}

    def with_(**over: Any) -> dict[str, Any]:
        return {**good, **over}

    return [
        ("not increasing", with_(values=[9.99, 4.99])),
        ("equal values", with_(values=[4.99, 4.99])),
        ("zero count", with_(counts=[30, 0])),
        ("bool count", with_(counts=[30, True])),
        ("negative dropped", with_(dropped_count=-1)),
        ("bool dropped", with_(dropped_count=True)),
        ("lengths differ", with_(counts=[30])),
        ("empty", with_(values=[], counts=[])),
        ("outside edges", with_(values=[4.99, 123456.5])),
        ("unknown member", with_(extra=1)),
        ("integer float cannot hold", with_(values=[4.99, 10**400])),
    ]


@pytest.mark.parametrize(("label", "block"), _bad_blocks(), ids=[b[0] for b in _bad_blocks()])
def test_malformed_exact_values_are_refused(label: str, block: Any) -> None:
    snap, stats = _fit(_prices(2_000))
    stats["exact_values"] = block
    with pytest.raises(StatisticalSpecError) as caught:
        load_spec({"name": "x"}, snapshot=snap)
    err = caught.value
    assert err.code == "statistical_stats_degenerate"
    assert "123456" not in str(err)
    assert err.__cause__ is None
    assert err.__context__ is None


def _refused_exact(block: dict[str, Any], **stats_over: Any) -> None:
    snap, stats = _fit(_prices(2_000))
    stats["exact_values"] = block
    stats.update(stats_over)
    with pytest.raises(StatisticalSpecError) as caught:
        load_spec({"name": "x"}, snapshot=snap)
    err = caught.value
    assert err.code == "statistical_stats_degenerate"
    assert "999999" not in str(err)
    assert err.__cause__ is None
    assert err.__context__ is None


def test_exact_count_below_the_floor_is_refused() -> None:
    _refused_exact({"values": [4.99, 9.99], "counts": [1_991, 9], "dropped_count": 0})


def test_exact_giant_count_is_refused() -> None:
    _refused_exact({"values": [4.99, 9.99], "counts": [10**400, 1_000], "dropped_count": 0})


def test_exact_mass_mismatch_is_refused() -> None:
    _refused_exact({"values": [4.99, 9.99], "counts": [1_000, 900], "dropped_count": 0})
    _refused_exact({"values": [4.99, 9.99], "counts": [1_000, 1_000], "dropped_count": 5})


def test_exact_bool_counts_are_refused() -> None:
    _refused_exact({"values": [4.99, 9.99], "counts": [1_999, True], "dropped_count": 0})
    _refused_exact({"values": [4.99, 9.99], "counts": [1_000, 1_000], "dropped_count": False})


def test_exact_bad_bin_counts_are_refused() -> None:
    block = {"values": [4.99, 9.99], "counts": [1_000, 1_000], "dropped_count": 0}
    _refused_exact(block, bin_counts=[1_000, True, 999])
    _refused_exact(block, bin_counts=[2_001, -1])
    _refused_exact(block, bin_counts=[])


def test_a_valid_fit_round_trips_through_the_floor_checks() -> None:
    snap, stats = _fit(_prices())
    load_spec({"name": "x"}, snapshot=snap)
    grid_snap, grid_stats = _fit(np.random.default_rng(1).normal(0, 1, 120))
    assert "quantile_grid" in grid_stats
    load_spec({"name": "x"}, snapshot=grid_snap)


def test_both_keys_present_are_refused() -> None:
    snap, stats = _fit(_prices(2_000))
    stats["quantile_grid"] = {"probs": [0.1, 0.9], "values": [5.0, 40.0]}
    with pytest.raises(StatisticalSpecError) as caught:
        load_spec({"name": "x"}, snapshot=snap)
    assert caught.value.code == "statistical_stats_degenerate"
    assert caught.value.__context__ is None


def test_a_value_within_rounding_of_the_edge_loads() -> None:
    snap, stats = _fit(_prices(2_000))
    stats["bin_edges"] = [0.5, 1.0]
    stats["exact_values"] = {
        "values": [0.5, 1.0000000000004],
        "counts": [1_000, 1_000],
        "dropped_count": 0,
    }
    load_spec({"name": "x"}, snapshot=snap)


def test_dp_carrier_ignores_exact_values() -> None:
    from decoy_engine.generation.statistical import sample_column

    snap, stats = _fit(_prices(2_000))
    assert "exact_values" in stats
    snap["columns"]["x"]["carrier"] = "number"
    plain = json.loads(json.dumps(snap))
    del plain["columns"]["x"]["stats"]["exact_values"]
    kept = sample_column(load_spec({"name": "x"}, snapshot=snap, dp_verified=True), 500, col_seed=3)
    assert kept == sample_column(
        load_spec({"name": "x"}, snapshot=plain, dp_verified=True), 500, col_seed=3
    )


def _shape_of(stats: dict[str, Any]) -> str:
    return "exact" if "exact_values" in stats else "grid" if "quantile_grid" in stats else "none"


def test_exact_mode_threshold() -> None:
    def numeric(counts: list[int]) -> dict[str, Any]:
        values = np.repeat(np.arange(len(counts), dtype=np.int64) * 7, counts)
        return _fit(values)[1]

    assert _shape_of(numeric([20] * 30)) == "exact"
    assert "quantile_grid" not in numeric([20] * 30)
    assert _shape_of(numeric([20] * 31)) == "grid"
    rare = numeric([10] * 25 + [1] * 5)
    assert _shape_of(rare) == "grid"
    assert "exact_values" not in rare

    def kind(n_distinct: int) -> str:
        frame = pd.DataFrame({"x": [f"v{i % n_distinct}" for i in range(600)]})
        return str(fit(frame, cell_format=False)["columns"]["x"]["kind"])

    assert kind(30) == "categorical"
    assert kind(31) == "freetext"
