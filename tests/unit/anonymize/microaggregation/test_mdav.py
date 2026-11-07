"""MDAV partition: hand-computed fixtures, loop boundaries, ties, scaling."""

from __future__ import annotations

from fractions import Fraction

import numpy as np
import pandas as pd
import pytest

from decoy_engine.anonymize.microaggregation import microaggregate
from decoy_engine.anonymize.microaggregation._mdav import mdav_groups, standardise

HAND = pd.DataFrame(
    {
        "x": [0, 1, 2, 3, 10, 11, 12],
        "y": [0, 2, 3, 1, 12, 10, 11],
    }
)


def _groups(x: np.ndarray, k: int) -> list[int]:
    matrix = np.asarray(x, dtype=np.float64)
    if matrix.ndim == 1:
        matrix = matrix[:, None]
    z, _ = standardise(matrix, [f"c{i}" for i in range(matrix.shape[1])])
    return mdav_groups(z, k).tolist()


def _raw_groups(x: object, k: int) -> list[int]:
    matrix = np.asarray(x, dtype=np.float64)
    if matrix.ndim == 1:
        matrix = matrix[:, None]
    return mdav_groups(np.ascontiguousarray(matrix), k).tolist()


def test_hand_fixture_groups_and_centroids() -> None:
    # Pass 1 (7 >= 3k): x_r = r6, x_s = r0; {r5, r6} then {r0, r1}; 3 left < 2k: last group.
    assert _groups(HAND.to_numpy(), 2) == [1, 1, 2, 2, 2, 0, 0]
    out = microaggregate(HAND, ["x", "y"], k=2).table
    assert out["x"].tolist() == [0.5, 0.5, 5.0, 5.0, 5.0, 11.5, 11.5]
    assert out["y"].tolist() == [1.0, 1.0, *([np.float64(16) / 3] * 3), 10.5, 10.5]
    assert out["x"].dtype == np.float64
    assert out["y"].dtype == np.float64


def test_hand_fixture_report() -> None:
    report = microaggregate(HAND, ["x", "y"], k=2).report
    assert report.row_count == 7
    assert report.k == 2
    assert report.quasi_identifiers == ("x", "y")
    assert report.group_count == 3
    assert report.group_size_min == 2
    assert report.group_size_max == 3
    assert report.group_size_distribution == ((2, 2), (3, 1))
    variance = Fraction(1132, 49)
    sse_x = Fraction(39) / variance
    sse_y = Fraction(427, 6) / variance
    assert report.sse == pytest.approx(float(sse_x + sse_y), rel=1e-12)
    assert float(sse_x + sse_y) == pytest.approx(32389 / 6792, rel=1e-15)
    assert report.sst == pytest.approx(14.0, rel=1e-12)
    assert report.information_loss == pytest.approx(32389 / 95088, rel=1e-12)
    assert report.column_information_loss == pytest.approx(
        (float(sse_x / 7), float(sse_y / 7)), rel=1e-12
    )


@pytest.mark.parametrize(
    ("n", "sizes"),
    [
        (3, [3]),
        (5, [5]),
        (6, [3, 3]),
        (8, [3, 5]),
        (9, [3, 3, 3]),
        (10, [3, 3, 4]),
        (15, [3, 3, 3, 3, 3]),
        (16, [3, 3, 3, 3, 4]),
    ],
)
def test_loop_boundaries(n: int, sizes: list[int]) -> None:
    x = np.random.default_rng(n).normal(size=(n, 2))
    groups = np.asarray(_groups(x, 3))
    counts = np.bincount(groups).tolist()
    assert sorted(counts) == sorted(sizes)
    # Groups are numbered in emission order; only the last one may exceed k.
    assert all(c == 3 for c in counts[:-1])


def test_second_group_anchors_on_x_s_chosen_before_first_removal() -> None:
    # Every row is 625 from r0, so x_s = r1 ties into the first group; sdcMicro still
    # anchors the second group on r1's coordinates.
    z = [(0, 0), (-7, 24), (0, 25), (7, 24), (-15, 20), (15, 20)]
    assert _raw_groups(z, 2) == [0, 0, 1, 2, 1, 2]


def test_ties_identical_rows() -> None:
    assert _raw_groups([[7.0]] * 5, 2) == [0, 0, 1, 1, 1]


def test_ties_farthest_takes_earliest_row() -> None:
    assert _raw_groups([0, 10, 5, 5], 2) == [0, 1, 0, 1]


def test_ties_nearest_takes_earliest_row() -> None:
    assert _raw_groups([(0, 1), (1, 0), (0, 0), (9, 9)], 2) == [0, 1, 1, 0]
    assert _groups(np.array([(0, 1), (1, 0), (0, 0), (9, 9)]), 2) == [0, 1, 1, 0]


def _scaled_fixture() -> np.ndarray:
    rng = np.random.default_rng(0)
    return np.column_stack([rng.normal(size=30), rng.normal(size=30) * 1000.0])


def test_standardisation_matters_on_the_fixture() -> None:
    x = _scaled_fixture()
    assert _groups(x, 3) != _raw_groups(x - x.mean(axis=0), 3)


@pytest.mark.parametrize("factor", [1e6, 1e-6, 1.0, 3.0])
def test_scale_invariance(factor: float) -> None:
    x = _scaled_fixture()
    scaled = x.copy()
    scaled[:, 1] *= factor
    assert _groups(scaled, 3) == _groups(x, 3)


def _offset_fixture(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.column_stack([rng.normal(size=40), rng.permutation(40).astype(np.float64)])


@pytest.mark.parametrize("offset", [1e9, 1e15])
@pytest.mark.parametrize("seed", range(10))
def test_translation_invariance_on_offset_column(offset: float, seed: int) -> None:
    # Epoch-like offsets: without centring, z-scores near 1e14 lose the digits that
    # separate rows (1e15 is exact in float64 for these integers).
    x = _offset_fixture(seed)
    shifted = x.copy()
    shifted[:, 1] += offset
    assert _groups(shifted, 3) == _groups(x, 3)


def test_centring_matters_on_the_offset_fixture() -> None:
    def uncentred(x: np.ndarray) -> list[int]:
        return mdav_groups(np.ascontiguousarray(x / x.std(axis=0)), 3).tolist()

    differs = 0
    for seed in range(10):
        shifted = _offset_fixture(seed)
        shifted[:, 1] += 1e15
        differs += uncentred(shifted) != _groups(_offset_fixture(seed), 3)
    assert differs > 0


def test_constant_non_dyadic_column_passes_through_exactly() -> None:
    # 0.1 has no exact binary form: a std test sees 1.39e-17 and a mean != 0.1.
    frame = pd.DataFrame({"a": [5.0, 1.0, 9.0, 2.0, 7.0], "c": [0.1] * 5})
    result = microaggregate(frame, ["a", "c"], k=2)
    assert result.table["c"].tolist() == [0.1] * 5
    assert result.report.column_information_loss[1] == 0.0
    _, constant = standardise(frame.to_numpy(dtype=np.float64), ["a", "c"])
    assert constant.tolist() == [False, True]


def test_constant_column_does_not_change_the_partition() -> None:
    x = _scaled_fixture()
    with_constant = np.column_stack([x, np.full(len(x), 0.1)])
    assert _groups(with_constant, 3) == _groups(x, 3)


def test_all_columns_constant() -> None:
    frame = pd.DataFrame({"a": [0.1] * 5, "b": [3] * 5})
    result = microaggregate(frame, ["a", "b"], k=2)
    assert result.table["a"].tolist() == [0.1] * 5
    assert result.table["b"].tolist() == [3.0] * 5
    assert result.report.group_size_distribution == ((2, 1), (3, 1))
    assert result.report.sse == 0.0
    assert result.report.sst == 0.0
    assert result.report.information_loss == 0.0
    assert result.report.column_information_loss == (0.0, 0.0)
