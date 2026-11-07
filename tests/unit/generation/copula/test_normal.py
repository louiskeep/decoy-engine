"""A1: the vectorized normal CDF and inverse against a committed 50-digit reference table.

Bounds scale with the functions' conditioning (`max(1, z^2)`); the comparison with
`statistics.NormalDist().inv_cdf` is a faithful-port check (both are AS241), not an accuracy check.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pytest

from decoy_engine.generation.copula._normal import ndtr, ndtri

_REF = json.loads(Path(__file__).with_name("_normal_reference.json").read_text(encoding="utf-8"))
_TINY = (
    2.2250738585072014e-308  # below this the CDF is subnormal and only absolute error is meaningful
)


def _cdf_table() -> tuple[np.ndarray, np.ndarray]:
    z = np.array([float(a) for a, _ in _REF["cdf"]])
    ref = np.array([float(Decimal(b)) for _, b in _REF["cdf"]])
    return z, ref


def _inv_table() -> tuple[np.ndarray, np.ndarray]:
    p = np.array([float(a) for a, _ in _REF["inv"]])
    ref = np.array([float(Decimal(b)) for _, b in _REF["inv"]])
    return p, ref


def test_reference_table_covers_the_range() -> None:
    z, _ = _cdf_table()
    p, _ = _inv_table()
    assert z.min() <= -37.5 and z.max() >= 9.0 and len(z) > 500
    assert p.min() <= 1e-300 and p.max() >= 1 - 1e-15 and len(p) > 500


def test_ndtr_relative_error_scaled_by_conditioning() -> None:
    z, ref = _cdf_table()
    got = ndtr(z)
    normal = ref >= _TINY
    bound = 2e-15 * np.maximum(1.0, z**2)
    rel = np.abs(got[normal] - ref[normal]) / ref[normal]
    assert np.all(rel <= bound[normal]), float(np.max(rel / bound[normal]))
    assert np.all(np.abs(got[~normal] - ref[~normal]) <= 1e-320)


def test_ndtri_error_scaled_by_conditioning() -> None:
    p, ref = _inv_table()
    got = ndtri(p)
    bound = 2e-15 * np.maximum(1.0, np.abs(ref)) + 1e-300
    assert np.all(np.abs(got - ref) <= bound), float(np.max(np.abs(got - ref) / bound))


def test_round_trip_within_tail_conditioning() -> None:
    p, ref = _inv_table()
    interior = p < 0.5  # the upper half is limited by the spacing of doubles near 1, not the code
    back = ndtr(ndtri(p[interior]))
    bound = 4e-15 * np.maximum(1.0, ref[interior] ** 2) * p[interior]
    assert np.all(np.abs(back - p[interior]) <= bound)


def test_ndtri_is_a_faithful_as241_port() -> None:
    p, _ = _inv_table()
    oracle = np.array([NormalDist().inv_cdf(v) for v in p])
    ulps = np.abs(ndtri(p) - oracle) / np.spacing(np.abs(oracle) + 1e-300)
    assert np.all(ulps <= 4)


def test_ndtr_matches_normaldist_where_it_has_no_cancellation() -> None:
    z = np.linspace(-8.0, 8.0, 16001)
    oracle = np.array([NormalDist().cdf(v) for v in z])
    assert np.max(np.abs(ndtr(z) - oracle)) <= 1e-15


def test_ndtr_special_values() -> None:
    out = ndtr(np.array([-np.inf, np.inf, np.nan, 0.0, -40.0, 40.0]))
    assert out[0] == 0.0 and out[1] == 1.0 and np.isnan(out[2]) and out[3] == 0.5
    assert out[4] == 0.0 and out[5] == 1.0


def test_ndtr_is_elementwise() -> None:
    z = np.linspace(-10, 10, 1001)
    whole = ndtr(z)
    assert all(ndtr(z[i : i + 1])[0] == whole[i] for i in range(0, 1001, 37))


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5, float("nan")])
def test_ndtri_refuses_outside_the_open_interval(bad: float) -> None:
    with pytest.raises(ValueError, match="strictly between 0 and 1") as info:
        ndtri(np.array([0.3, bad]))
    assert info.value.__cause__ is None and info.value.__context__ is None
