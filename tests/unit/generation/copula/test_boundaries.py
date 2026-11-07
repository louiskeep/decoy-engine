"""A11: uniforms never reach 0 or 1, and every marginal lookup accepts the closed interval."""

from __future__ import annotations

import numpy as np
import pytest

from decoy_engine.generation.copula._marginals import numeric_inverse
from decoy_engine.generation.copula._model import KIND_NUMERIC, ColumnMarginal
from decoy_engine.generation.copula._normal import ndtri
from decoy_engine.generation.copula._rng import raw_to_uniform

_EDGE_U = np.array([0.0, 1.0, np.nextafter(1.0, 0.0), 5e-324, 0.5])


def test_raw_extremes_map_strictly_inside_the_unit_interval() -> None:
    raw = np.array([0, 2**64 - 1, 2**63], dtype=np.uint64)
    u = raw_to_uniform(raw)
    assert u[0] == 2.0**-53 and u[1] == 1.0 - 2.0**-53
    assert np.all((u > 0) & (u < 1))
    assert np.all(np.isfinite(ndtri(u)))


def _numeric(edges: list[float], counts: list[int]) -> ColumnMarginal:
    return ColumnMarginal("v", KIND_NUMERIC, "float64", 0, sum(counts), tuple(edges), tuple(counts))


@pytest.mark.parametrize(
    ("edges", "counts"),
    [
        ([0.0, 1.0, 2.0, 3.0, 4.0], [0, 5, 7, 0]),  # leading and trailing empty bins
        ([0.0, 1.0, 2.0], [100, 1]),  # tiny last bin
        ([2.5, 2.5], [9]),  # constant column
        ([0.0, 1e-300, 1.0], [3, 0]),
    ],
)
def test_numeric_lookup_lands_in_a_positive_bin(edges: list[float], counts: list[int]) -> None:
    column = _numeric(edges, counts)
    values = numeric_inverse(column, _EDGE_U)
    occupied = [(edges[i], edges[i + 1]) for i, c in enumerate(counts) if c > 0]
    for v in values:
        assert any(lo <= v <= hi for lo, hi in occupied), v


def test_u_times_total_rounding_up_is_clamped() -> None:
    column = _numeric([0.0, 1.0, 2.0, 3.0], [3, 3, 0])
    top = numeric_inverse(column, np.array([1.0, np.nextafter(1.0, 0.0)]))
    assert np.all(top <= 2.0)
