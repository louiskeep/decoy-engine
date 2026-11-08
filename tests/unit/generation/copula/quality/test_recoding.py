"""Recoding: states under the floor merge before tabulation, decided from real counts only."""

from __future__ import annotations

import numpy as np
import pandas as pd

from decoy_engine.generation.copula._marginals import from_snapshot_entry
from decoy_engine.generation.copula._model import ColumnMarginal
from decoy_engine.generation.copula._quality_inputs import discrete_states
from decoy_engine.generation.copula._quality_pairwise import contingency
from decoy_engine.quality.snapshot import compute_distribution_snapshot


def _marginal(series: pd.Series) -> ColumnMarginal:
    entry = compute_distribution_snapshot(series.to_frame("s"))["columns"]["s"]
    return from_snapshot_entry("s", entry)


def _counts(series: pd.Series, synthetic: pd.Series | None = None) -> list[int]:
    real_codes, _ = discrete_states(
        series, series if synthetic is None else synthetic, _marginal(series)
    )
    return sorted(int(c) for c in np.bincount(real_codes) if c)


def _labels(counts: dict[str, int]) -> pd.Series:
    return pd.Series([k for k, n in counts.items() for _ in range(n)], dtype=object)


def test_states_under_the_floor_fold_into_other() -> None:
    assert _counts(_labels({"A": 200, "B": 11, "C": 10, "D": 5, "E": 1})) == [11, 16, 200]


def test_small_other_merges_into_smallest() -> None:
    assert _counts(_labels({"A": 150, "B": 47, "C": 3})) == [50, 150]


def test_sparse_tail_bin_folds() -> None:
    values = pd.Series(np.r_[np.linspace(0.0, 0.5, 97), [1.0, 1.0, 1.0]])
    counts = _counts(values)
    assert min(counts) >= 11 and sum(counts) == 100


def test_small_null_state_folds() -> None:
    series = _labels({"A": 100, "B": 100})
    series.iloc[:3] = None
    counts = _counts(series)
    assert counts == [100, 100] and sum(counts) == 200


def test_fold_is_decided_from_real_counts() -> None:
    real = _labels({"A": 150, "B": 47, "C": 3})
    synthetic = _labels({"A": 50, "B": 50, "C": 100})
    real_codes, synth_codes = discrete_states(real, synthetic, _marginal(real))
    b_code = real_codes[real.to_numpy() == "B"][0]
    assert set(synth_codes[synthetic.to_numpy() == "C"]) == {b_code}
    assert set(real_codes[real.to_numpy() == "C"]) == {b_code}


def test_rare_combinations_do_not_move_the_recoded_score() -> None:
    # u: two large labels and twenty labels of 2 rows each; v: binary.
    u = _labels({"big1": 80, "big2": 80, **{f"r{i:02d}": 2 for i in range(20)}})
    rng = np.random.default_rng(11)
    v = pd.Series(rng.choice(np.array(["p", "q"], dtype=object), len(u)))
    reproduced = v.copy()
    shuffled = v.copy()
    rare = np.flatnonzero(u.str.startswith("r").to_numpy())
    # Permute v among the rare rows: the merged bucket keeps its composition, the pairs change.
    shuffled.iloc[rare] = v.iloc[rare].to_numpy()[np.random.default_rng(13).permutation(rare.size)]
    mu, mv = _marginal(u), _marginal(v)

    def scores(synth_v: pd.Series) -> tuple[float, float]:
        ru, su = discrete_states(u, u, mu)
        rv, sv = discrete_states(v, synth_v, mv)
        recoded = contingency(ru, rv, su, sv)
        raw = 1 - 0.5 * float(
            pd.crosstab(u, v, normalize=True)
            .sub(pd.crosstab(u, synth_v, normalize=True), fill_value=0)
            .abs()
            .to_numpy()
            .sum()
        )
        return recoded, raw

    recoded_a, raw_a = scores(reproduced)
    recoded_b, raw_b = scores(shuffled)
    # Unrecoded, 1 - TVD counts which rare (label, v) pairs the synthetic table reproduced.
    assert raw_a == 1.0 and raw_b < 1.0
    assert recoded_a == recoded_b == 1.0


def test_contingency_equals_sum_of_minimums() -> None:
    rng = np.random.default_rng(12)
    a, b = rng.integers(0, 4, 300), rng.integers(0, 3, 300)
    c, d = rng.integers(0, 4, 250), rng.integers(0, 3, 250)
    p = np.bincount(a * 3 + b, minlength=12) / 300
    q = np.bincount(c * 3 + d, minlength=12) / 250
    assert abs(contingency(a, b, c, d) - float(np.minimum(p, q).sum())) < 1e-12
