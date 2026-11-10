"""Quantile-grid sampling of fitted number columns (GEN-NUM-SHAPE, roadmap row 24u).

Bounds are fixed in the plan; a failure here means the sampler or the fit changed, not noise,
because every draw is seeded per row.
"""

from __future__ import annotations

import json
from itertools import pairwise
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine.generation.statistical import (
    StatisticalSpecError,
    cell_format_arrow_type,
    load_spec,
    sample_column,
)
from decoy_engine.generation.statistical._numeric_shape import grid_value
from decoy_engine.profiling import MIN_RELEASE_COUNT as K
from decoy_engine.profiling import distribution_snapshot_document
from decoy_engine.quality.snapshot import compute_distribution_snapshot
from tests.unit.generation.statistical._cf_support import draw, fit, similarity, spec_of

_REASON_DEGENERATE = "statistical_stats_degenerate"


def _fit(values: Any, **kw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    snap = fit(pd.DataFrame({"x": values}), cell_format=False, **kw)
    return snap, snap["columns"]["x"]["stats"]


def _outlier() -> np.ndarray:
    return np.append(np.random.default_rng(501).normal(100.0, 10.0, 19_999), 1_000_000.0)


def _lognormal() -> np.ndarray:
    return np.random.default_rng(502).lognormal(3.0, 1.2, 20_000)


def test_one_outlier_does_not_move_the_bulk() -> None:
    src = _outlier()
    snap, stats = _fit(src)
    assert "quantile_grid" in stats
    out = np.array(draw(snap))
    assert 95 <= out.mean() <= 105
    assert abs(np.median(out) - np.median(src)) <= 1.0
    assert out.max() <= 200
    assert out.max() <= sorted(src)[-K]
    assert similarity(snap, list(out)) >= 0.95


def test_lognormal_keeps_its_median() -> None:
    src = _lognormal()
    snap, stats = _fit(src)
    assert "quantile_grid" in stats
    out = np.array(draw(snap))
    assert abs(np.median(out) / np.median(src) - 1) <= 0.05
    assert abs(out.mean() / src.mean() - 1) <= 0.08
    assert abs(np.quantile(out, 0.95) / np.quantile(src, 0.95) - 1) <= 0.10
    assert similarity(snap, list(out)) >= 0.85


def test_a_far_cluster_of_k_rows_is_kept() -> None:
    src = np.append(
        np.random.default_rng(505).normal(100.0, 10.0, 19_950), np.full(50, 1_000_000.0)
    )
    snap, stats = _fit(src)
    assert "quantile_grid" in stats
    out = np.array(draw(snap))
    assert 0.8 <= out.mean() / src.mean() <= 1.2
    assert 0.001 <= (out > 200).mean() <= 0.01


def test_zero_inflated_keeps_its_zeros() -> None:
    src = np.append(np.zeros(12_000), np.random.default_rng(506).gamma(2.0, 30.0, 8_000))
    snap, stats = _fit(src)
    assert "quantile_grid" in stats
    out = np.array(draw(snap))
    assert abs((out == 0.0).mean() - 0.60) <= 0.01
    assert np.median(out) == 0
    assert similarity(snap, list(out)) >= 0.90


def test_a_normal_column_stays_good() -> None:
    snap, _ = _fit(np.random.default_rng(504).normal(50.0, 5.0, 20_000))
    assert similarity(snap, draw(snap)) >= 0.95


def test_grid_needs_one_hundred_rows() -> None:
    src = np.random.default_rng(507).normal(100, 10, 100)
    snap, stats = _fit(src)
    grid = stats["quantile_grid"]
    assert len(grid["probs"]) == 9
    assert grid["probs"][0] == pytest.approx(0.1)
    assert grid["probs"][-1] == pytest.approx(0.9)
    out = draw(snap, 5_000)
    assert min(out) >= sorted(src)[K - 1]
    assert max(out) <= sorted(src)[-K]
    _, short = _fit(src[:99])
    assert "quantile_grid" not in short


@pytest.mark.parametrize("mode", ["grid", "exact"])
def test_numeric_shape_windows_equal_a_full_run(mode: str) -> None:
    if mode == "grid":
        snap, stats = _fit(_lognormal())
        assert stats["quantile_grid"] is not None
    else:
        prices = np.random.default_rng(503).choice(
            [4.99, 9.99, 19.99, 49.99], p=[0.4, 0.3, 0.2, 0.1], size=20_000
        )
        snap, stats = _fit(prices)
        assert stats["exact_values"] is not None
    spec = spec_of(snap, "x")
    full = sample_column(spec, 5_100, col_seed=7)
    assert sample_column(spec, 5_100, col_seed=7) == full
    for k in (0, 1, 4_999):
        assert sample_column(spec, 100, col_seed=7, start=k) == full[k : k + 100]


def test_grid_typing_follows_445() -> None:
    ints, int_stats = _fit(np.random.default_rng(508).integers(0, 1_000, 5_000))
    assert "quantile_grid" in int_stats
    out = draw(ints, 2_000)
    assert all(type(v) is int for v in out)
    assert str(cell_format_arrow_type(spec_of(ints, "x"))) == "int64"
    floats, float_stats = _fit(np.random.default_rng(504).normal(50.0, 5.0, 20_000))
    assert "quantile_grid" in float_stats
    assert all(type(v) is float for v in draw(floats, 2_000))


def test_dp_carrier_ignores_a_grid() -> None:
    src = np.random.default_rng(504).normal(50.0, 5.0, 2_000)
    snap, stats = _fit(src)
    assert "quantile_grid" in stats
    carrier_snap = json.loads(json.dumps(snap))
    carrier_snap["columns"]["x"]["carrier"] = "number"
    plain = json.loads(json.dumps(carrier_snap))
    del plain["columns"]["x"]["stats"]["quantile_grid"]
    with_grid = sample_column(
        load_spec({"name": "x"}, snapshot=carrier_snap, dp_verified=True), 500, col_seed=3
    )
    without = sample_column(
        load_spec({"name": "x"}, snapshot=plain, dp_verified=True), 500, col_seed=3
    )
    assert with_grid == without


def test_grid_is_small() -> None:
    _, stats = _fit(_outlier())
    assert "quantile_grid" in stats
    assert len(json.dumps(stats["quantile_grid"])) <= 3_000
    assert len(stats["quantile_grid"]["probs"]) <= 103


def _malformed_cases() -> list[tuple[str, Any]]:
    return [
        ("probs not increasing", lambda g: g.update(probs=[0.1, 0.5, 0.4], values=[1.0, 2.0, 3.0])),
        ("prob above one", lambda g: g.update(probs=[0.1, 1.5], values=[1.0, 2.0])),
        ("prob below zero", lambda g: g.update(probs=[-0.1, 0.5], values=[1.0, 2.0])),
        ("values decreasing", lambda g: g.update(probs=[0.1, 0.5, 0.9], values=[3.0, 2.0, 1.0])),
        ("lengths differ", lambda g: g.update(probs=[0.1, 0.5, 0.9], values=[1.0, 2.0])),
        ("one point", lambda g: g.update(probs=[0.5], values=[1.0])),
        ("bool member", lambda g: g.update(probs=[0.1, 0.9], values=[True, 2.0])),
        ("string member", lambda g: g.update(probs=[0.1, 0.9], values=["1", 2.0])),
        ("value outside edges", lambda g: g.update(probs=[0.1, 0.9], values=[1.0, 1e9])),
        ("unknown member", lambda g: g.update(extra=1)),
        ("integer float cannot hold", lambda g: g.update(probs=[0.1, 0.9], values=[1.0, 10**400])),
        ("not an object", lambda g: None),
    ]


@pytest.mark.parametrize(
    ("label", "mutate"), _malformed_cases(), ids=[c[0] for c in _malformed_cases()]
)
def test_malformed_grid_is_refused(label: str, mutate: Any) -> None:
    snap, stats = _fit(np.random.default_rng(504).normal(50.0, 5.0, 500))
    assert "quantile_grid" in stats
    if label == "not an object":
        stats["quantile_grid"] = [1, 2]
    else:
        mutate(stats["quantile_grid"])
    with pytest.raises(StatisticalSpecError) as caught:
        load_spec({"name": "x"}, snapshot=snap)
    err = caught.value
    assert getattr(err, "code", None) == _REASON_DEGENERATE
    assert "1000000000" not in str(err)
    assert err.__cause__ is None
    assert err.__context__ is None


@pytest.mark.parametrize(
    ("label", "probs"),
    [
        ("undersized interval", [0.1, 0.12, 0.9]),
        ("undersized low tail", [0.001, 0.5, 0.9]),
        ("undersized high tail", [0.1, 0.5, 0.999]),
    ],
)
def test_undersized_grid_interval_or_tail_is_refused(label: str, probs: list[float]) -> None:
    snap, stats = _fit(np.random.default_rng(504).normal(50.0, 5.0, 100))
    stats["quantile_grid"] = {"probs": probs, "values": [40.0, 45.0, 55.0][: len(probs)]}
    with pytest.raises(StatisticalSpecError) as caught:
        load_spec({"name": "x"}, snapshot=snap)
    assert caught.value.code == _REASON_DEGENERATE
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_undersized_tail_on_a_hundred_rows_is_refused() -> None:
    snap, stats = _fit(np.random.default_rng(504).normal(50.0, 5.0, 100))
    stats["quantile_grid"] = {"probs": [0.001, 0.999], "values": [40.0, 60.0]}
    with pytest.raises(StatisticalSpecError):
        load_spec({"name": "x"}, snapshot=snap)


@pytest.mark.parametrize(
    ("label", "mass", "tail_rows"),
    [("mass above the cap", 2**54, 1), ("two-row tail at the cap", 2**40, 2)],
)
def test_huge_claimed_mass_cannot_hide_a_small_tail(label: str, mass: int, tail_rows: int) -> None:
    snap, stats = _fit(np.random.default_rng(504).normal(50.0, 5.0, 100))
    stats["bin_counts"] = [mass // 2, mass - mass // 2]
    stats["quantile_grid"] = {"probs": [tail_rows / mass, 0.5], "values": [40.0, 50.0]}
    with pytest.raises(StatisticalSpecError) as caught:
        load_spec({"name": "x"}, snapshot=snap)
    assert caught.value.code == _REASON_DEGENERATE


def test_equal_neighbouring_grid_values_load() -> None:
    snap, stats = _fit(np.random.default_rng(504).normal(50.0, 5.0, 500))
    grid = stats["quantile_grid"]
    grid["values"][1] = grid["values"][0]
    spec = load_spec({"name": "x"}, snapshot=snap)
    assert len(sample_column(spec, 50, col_seed=1)) == 50


@settings(
    max_examples=200, derandomize=True, deadline=None, suppress_health_check=list(HealthCheck)
)
@given(
    n=st.integers(100, 2_000),
    seed=st.integers(0, 10_000),
    extremes=st.lists(st.sampled_from([-1e12, 1e12]), max_size=5),
)
def test_grid_floor_properties(n: int, seed: int, extremes: list[float]) -> None:
    src = np.random.default_rng(seed).normal(0.0, 100.0, n)
    src[: len(extremes)] = extremes
    snap = compute_distribution_snapshot(pd.DataFrame({"x": src}))
    stats = snap["columns"]["x"]["stats"]
    if "quantile_grid" not in stats:
        pytest.skip("low-distinct column took exact mode")
    p, v = stats["quantile_grid"]["probs"], stats["quantile_grid"]["values"]
    ordered = sorted(src)
    assert all(b > a for a, b in pairwise(p))
    assert all(b >= a for a, b in pairwise(v))
    assert all((b - a) * n >= K - 1e-9 for a, b in pairwise(p))
    assert v[0] >= ordered[K - 1]
    assert v[-1] <= ordered[-K]
    assert stats["bin_edges"][0] <= v[0]
    assert v[-1] <= stats["bin_edges"][-1]
    spec = load_spec({"name": "x"}, snapshot=json.loads(json.dumps(snap)))
    out = sample_column(spec, 1_000, col_seed=5)
    assert all(v[0] <= x <= v[-1] for x in out)


def test_grid_value_math() -> None:
    probs, values = [0.1, 0.5, 0.9], [0.0, 10.0, 20.0]
    assert grid_value(probs, values, 0.0) == 0
    assert grid_value(probs, values, 0.25) == 5
    assert grid_value(probs, values, 0.5) == 10
    assert grid_value(probs, values, 0.75) == 15
    assert grid_value(probs, values, 0.999) == pytest.approx(20, abs=0.1)


@pytest.mark.parametrize("case", ["a", "b"])
def test_grid_values_stay_inside_the_edges(case: str) -> None:
    if case == "a":
        src = np.append(1.0 + 0.01 * np.arange(200), np.full(20, 20 / 3))
    else:
        src = np.append(0.5 + 0.002 * np.arange(200), np.full(20, 1.0000000000004))
    snap, stats = _fit(src)
    v, edges = stats["quantile_grid"]["values"], stats["bin_edges"]
    if case == "a":
        assert v[-1] == 20 / 3
    else:
        assert v[-1] == edges[-1] == 1.0
    assert v[0] >= sorted(src)[K - 1]
    assert all(edges[0] <= x <= edges[-1] for x in v)
    load_spec({"name": "x"}, snapshot=snap)


def test_thirteen_digit_integers_keep_the_floor_after_integer_formatting() -> None:
    src = 1_234_567_890_123 + np.arange(500)
    snap, stats = _fit(src)
    assert stats["quantile_grid"]["values"][0] == pytest.approx(1_234_567_890_132.98)
    out = draw(snap, 2_000)
    assert all(type(x) is int for x in out)
    assert all(sorted(src)[K - 1] <= x <= sorted(src)[-K] for x in out)


def test_all_distinct_grid_has_interval_mass_and_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    src = np.arange(101)
    snap, stats = _fit(src)
    probs = stats["quantile_grid"]["probs"]
    ends = [probs[0], 1 - probs[-1]]
    assert all(m >= K / 101 - 1e-9 for m in ends)
    assert all(b - a >= K / 101 - 1e-9 for a, b in pairwise(probs))
    assert all(sorted(src)[K - 1] <= x <= sorted(src)[-K] for x in draw(snap, 2_000))
    from decoy_engine.profiling import _snapshot_values as sv

    monkeypatch.setattr(sv, "value_sensitive_names", lambda fields: [f["name"] for f in fields])
    doc = distribution_snapshot_document(pd.DataFrame({"x": src}))
    assert "quantile_grid" in doc["columns"]["x"]["stats"]
    assert doc["columns"]["x"]["stats"]["quantile_grid"] is None


@pytest.mark.parametrize("case", ["exact", "grid"])
def test_int64_above_two_to_the_53(case: str) -> None:
    if case == "exact":
        # 2**53 and 2**53 + 1 share a float64: they must stay two values. (The plan's literal
        # [+1, +3, +5] input is a range float64 cannot split into ten histogram bins, which
        # fails inside the existing numeric_stats before this row's code runs.)
        wanted = [2**53, 2**53 + 1, 2**53 + 3, 2**53 + 1001]
        src = np.array(wanted * 40, dtype=np.int64)
        snap, stats = _fit(src)
        assert stats["exact_values"]["values"] == wanted
        assert all(type(x) is int for x in stats["exact_values"]["values"])
        assert set(draw(snap, 2_000)) == set(wanted)
    else:
        src = 2**53 + 2 * np.arange(500, dtype=np.int64)
        snap, _ = _fit(src)
        out = draw(snap, 2_000)
        ordered = sorted(int(x) for x in src)
        assert all(type(x) is int for x in out)
        assert float(min(out)) >= float(ordered[K - 1])
        assert float(max(out)) <= float(ordered[-K])


# Digests of the histogram path recorded on engine main 53632b57, before this row. The same
# four fits with both new keys deleted must still draw these bytes.
_OLD_SNAPSHOT_DIGESTS = {
    "normal": "6ae5fba3e810435cb32b73848e9f1c3962d94115c014130f73a78ecc315d3d5d",
    "int_cell_format": "ef7670ecd06814ce45575a89f9c979687c0a66e7cfeb81099c595e35aeb10558",
    "outlier": "0ef0606dfe7f86893dacd8186f9e5d879c7cfc236a304202df7e38570a16e3c7",
    "four_prices": "c01323b395d4bd9c474c519782f9caf45e154faca426582c58f3cad9a721d8c3",
}


def _old_snapshot_sources() -> dict[str, tuple[Any, bool]]:
    rng = np.random.default_rng
    return {
        "normal": (rng(504).normal(50.0, 5.0, 20_000), False),
        "int_cell_format": (rng(508).integers(0, 1_000, 5_000), True),
        "outlier": (np.append(rng(501).normal(100.0, 10.0, 19_999), 1_000_000.0), False),
        "four_prices": (
            rng(503).choice([4.99, 9.99, 19.99, 49.99], p=[0.4, 0.3, 0.2, 0.1], size=20_000),
            False,
        ),
    }


@pytest.mark.parametrize("name", sorted(_OLD_SNAPSHOT_DIGESTS))
def test_old_snapshot_is_byte_identical(name: str) -> None:
    import hashlib

    src, cell_format = _old_snapshot_sources()[name]
    snap = fit(pd.DataFrame({"x": src}), cell_format=cell_format)
    for key in ("quantile_grid", "exact_values"):
        snap["columns"]["x"]["stats"].pop(key, None)
    values = sample_column(load_spec({"name": "x"}, snapshot=snap), 2_000, col_seed=7)
    assert hashlib.sha256(repr(values).encode()).hexdigest() == _OLD_SNAPSHOT_DIGESTS[name]
