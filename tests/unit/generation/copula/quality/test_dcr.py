"""A4: the chunked DCR against a brute-force oracle, its floors, samples and memory bound."""

from __future__ import annotations

import tracemalloc

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.copula._quality_dcr import (
    DistanceSpec,
    encode,
    nearest,
    summarize,
)
from decoy_engine.generation.copula._quality_inputs import canonical, sample_rows
from decoy_engine.quality.synth_report import compute_dcr

from .._fixtures import mixed_frame
from ._brute_dcr import brute_nearest
from ._helpers import fit, report, sample

_SPEC = DistanceSpec(("n1", "n0", "c", "n2"), (True, True, False, True), (2.0, 0.0, 0.0, 0.5))


def _random_frame(rng: np.random.Generator, rows: int) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "n1": np.round(rng.normal(size=rows), 1),  # ties
            "n0": np.where(rng.random(rows) < 0.5, 3.0, 4.0),  # compared under a zero range
            "c": rng.choice(np.array(["a", "b", "c"], dtype=object), rows),
            "n2": rng.normal(size=rows) * 2,  # often beyond its range of 0.5: clips at 1
        }
    )
    for name in frame.columns:
        mask = rng.random(rows) < 0.15
        frame.loc[mask, name] = None if name == "c" else np.nan
    return frame


@pytest.mark.parametrize("seed", range(20))
def test_matches_brute_force(seed: int) -> None:
    rng = np.random.default_rng(seed)
    query, reference = _random_frame(rng, 23), _random_frame(rng, 31)
    q, r = encode([query, reference], _SPEC)
    fast = nearest(q, r, _SPEC)
    slow = brute_nearest(query, reference, _SPEC.columns, _SPEC.numeric, _SPEC.ranges)
    assert np.allclose(fast, slow, rtol=0, atol=1e-12)
    (only,) = encode([reference], _SPEC)
    self_slow = brute_nearest(
        reference, reference, _SPEC.columns, _SPEC.numeric, _SPEC.ranges, exclude_self=True
    )
    # Block 7 over 31 rows: self-exclusion must hold in every block, not only the first.
    for block in (7, 256):
        self_fast = nearest(only, only, _SPEC, exclude_self=True, block=block)
        assert np.allclose(self_fast, self_slow, rtol=0, atol=1e-12)


def test_block_size_does_not_change_the_result() -> None:
    rng = np.random.default_rng(99)
    q, r = encode([_random_frame(rng, 600), _random_frame(rng, 300)], _SPEC)
    results = [nearest(q, r, _SPEC, block=b) for b in (1, 7, 256)]
    assert all(np.array_equal(results[0], other) for other in results[1:])
    selfs = [nearest(r, r, _SPEC, exclude_self=True, block=b) for b in (1, 7, 256)]
    assert all(np.array_equal(selfs[0], other) for other in selfs[1:])
    slow = brute_nearest(
        *(_random_frame(np.random.default_rng(98), 300),) * 2,
        _SPEC.columns,
        _SPEC.numeric,
        _SPEC.ranges,
        exclude_self=True,
    )
    (m,) = encode([_random_frame(np.random.default_rng(98), 300)], _SPEC)
    assert np.allclose(nearest(m, m, _SPEC, exclude_self=True), slow, rtol=0, atol=1e-12)


def test_null_rules() -> None:
    spec = DistanceSpec(("x", "c"), (True, False), (10.0, 0.0))
    q = pd.DataFrame({"x": [np.nan, np.nan, 1.0], "c": [None, "a", None]}, dtype=object)
    q["x"] = q["x"].astype(float)
    r = pd.DataFrame({"x": [np.nan], "c": [None]}, dtype=object)
    r["x"] = r["x"].astype(float)
    qm, rm = encode([q, r], spec)
    # both null 0 in each kind; one null 1
    assert list(nearest(qm, rm, spec)) == [0.0, 0.5, 0.5]


def test_systematic_sample() -> None:
    assert list(sample_rows(12, 5)) == [0, 2, 4, 7, 9]
    assert list(sample_rows(4, 5)) == [0, 1, 2, 3]


@pytest.mark.parametrize(
    ("rows", "released"),
    [
        (21, set()),
        (22, {"median"}),
        (43, {"median"}),
        (44, {"median", "p25", "p75"}),
        (219, {"median", "p25", "p75"}),
        (220, {"median", "p25", "p75", "p05", "p95"}),
    ],
)
def test_percentile_floor(rows: int, released: set[str]) -> None:
    summary = summarize(np.linspace(0.0, 1.0, rows), rows)
    values = {"p05": summary.p05, "p25": summary.p25, "median": summary.median}
    values |= {"p75": summary.p75, "p95": summary.p95}
    assert {k for k, v in values.items() if v is not None} == released
    assert dict(summary.withheld) == {k: "percentile_rows" for k in values if k not in released}
    assert summary.rows == rows


def test_isolated_group_is_not_released_by_the_baseline() -> None:
    # 94 rows in a tight cluster, 6 isolated rows: unfloored, p95 of real-to-real is theirs.
    rng = np.random.default_rng(14)
    values = np.r_[rng.normal(size=94) * 0.01, np.linspace(50.0, 55.0, 6)]
    spec = DistanceSpec(("x",), (True,), (float(values.max() - values.min()),))
    (m,) = encode([pd.DataFrame({"x": values})], spec)
    distances = nearest(m, m, spec, exclude_self=True)
    assert float(np.quantile(distances, 0.95)) > 0.005  # the leak an unfloored p95 would carry
    assert summarize(distances, 99).p95 is None


def test_floor_counts_the_smaller_side() -> None:
    summary = summarize(np.linspace(0.0, 1.0, 1000), 150)
    assert summary.p05 is None and summary.p95 is None and summary.p25 is not None
    real = mixed_frame(8, n=150)
    model = fit(real)
    result = report(real, model, sample(model, 1000))
    assert result.dcr is not None and result.dcr.synthetic_to_real.rows == 1000
    assert result.dcr.synthetic_to_real.p05 is None
    assert result.dcr.synthetic_to_real.p25 is not None


def test_baseline_excludes_only_self() -> None:
    real = pd.DataFrame({"x": np.linspace(0, 1, 120), "y": np.linspace(5, 9, 120) ** 2})
    model = fit(real)
    result = report(real, model)
    assert result.dcr is not None and result.dcr.real_to_real.p05 is None  # 120 rows < 220
    spec = DistanceSpec(("x", "y"), (True, True), (1.0, 64.0))
    (m,) = encode([real], spec)
    assert nearest(m, m, spec, exclude_self=True).min() > 0
    duplicated = real.copy()
    duplicated.iloc[1] = duplicated.iloc[0]
    (d,) = encode([duplicated], spec)
    assert nearest(d, d, spec, exclude_self=True).min() == 0.0


def test_holdout_share_is_strict() -> None:
    real = mixed_frame(3, n=400)
    model = fit(real)
    result = report(real, model, holdout=real.copy())
    assert result.dcr is not None and result.dcr.holdout is not None
    assert result.dcr.holdout.closer_to_real_share == 0.0
    assert result.dcr.holdout.protection_score == 1.0


def test_holdout_compares_equal_sized_samples() -> None:
    real = mixed_frame(4, n=1000)
    model = fit(real)
    synth = sample(model)
    holdout = mixed_frame(5, n=300)
    result = report(real, model, synth, holdout=holdout)
    assert result.dcr is not None and result.dcr.holdout is not None
    assert result.dcr.holdout.rows_compared == 300
    from decoy_engine.generation.copula._quality_dcr import spec_for

    spec = spec_for(model)
    frames = [canonical(f, model) for f in (real, synth, holdout)]
    r, s, h = encode([frames[0].iloc[sample_rows(1000, 300)], frames[1], frames[2]], spec)
    share = float(np.mean(nearest(s, r, spec) < nearest(s, h, spec)))
    assert result.dcr.holdout.closer_to_real_share == round(share, 3)


def test_max_rows_caps_every_side() -> None:
    real = mixed_frame(6, n=900)
    model = fit(real)
    result = report(real, model, sample(model, 700), dcr_max_rows=250)
    assert result.dcr is not None
    assert (result.dcr.real_rows_sampled, result.dcr.synthetic_rows_sampled) == (250, 250)
    assert result.dcr.synthetic_to_real.rows == 250


def test_agrees_with_compute_dcr_where_definitions_coincide() -> None:
    rng = np.random.default_rng(15)
    real = pd.DataFrame(
        {
            "x": rng.normal(size=500),
            "y": rng.gamma(2.0, size=500),
            "c": rng.choice(np.array(["a", "b", "c"], dtype=object), 500),
        }
    )
    model = fit(real)
    synth = sample(model)
    ours = report(real, model, synth).dcr
    theirs = compute_dcr(real, synth)["synth_to_source"]
    assert ours is not None
    for name in ("p05", "p25", "median", "p75", "p95"):
        assert getattr(ours.synthetic_to_real, name) == pytest.approx(theirs[name], abs=0.0011)


def test_copies_with_numeric_nulls_are_at_distance_zero() -> None:
    real = mixed_frame(7, n=500)
    model = fit(real)
    copy = real.copy()
    ours = report(real, model, copy).dcr
    assert ours is not None
    assert ours.exact_match_share == 1.0 and ours.synthetic_to_real.median == 0.0
    # The existing DCR scores numeric null/null as 1.0, so the same copies look distant there.
    assert compute_dcr(real, copy)["synth_to_source"]["p95"] > 0


def test_memory_is_bounded_by_the_block() -> None:
    rng = np.random.default_rng(16)
    columns = tuple(f"v{i}" for i in range(20))
    spec = DistanceSpec(columns, (True,) * 20, (1.0,) * 20)
    q = rng.normal(size=(5000, 20))
    r = rng.normal(size=(5000, 20))
    tracemalloc.start()
    try:
        nearest(q, r, spec)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 100 * 1024 * 1024
