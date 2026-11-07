"""Generated properties: group sizes, k-anonymity, totals, determinism, order."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.anonymize.microaggregation import microaggregate
from decoy_engine.anonymize.microaggregation._api import partition

PROPERTY_SETTINGS = settings(max_examples=150, deadline=None, derandomize=True)


@st.composite
def cases(draw: Any) -> tuple[pd.DataFrame, list[str], int]:
    k = draw(st.integers(2, 9))
    n = draw(st.one_of(st.just(0), st.integers(k, 300)))
    p = draw(st.integers(1, 4))
    columns: dict[str, Any] = {}
    for j in range(p):
        kind = draw(st.sampled_from(["small_int", "float", "constant"]))
        if kind == "small_int":
            values = draw(st.lists(st.integers(-3, 3), min_size=n, max_size=n))
            columns[f"c{j}"] = pd.Series(values, dtype="int64")
        elif kind == "float":
            values = draw(
                st.lists(
                    st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False),
                    min_size=n,
                    max_size=n,
                )
            )
            columns[f"c{j}"] = pd.Series(values, dtype="float64")
        else:
            columns[f"c{j}"] = pd.Series([0.1] * n, dtype="float64")
    return pd.DataFrame(columns), list(columns), k


@PROPERTY_SETTINGS
@given(cases())
def test_group_sizes_and_emission_order(case: tuple[pd.DataFrame, list[str], int]) -> None:
    frame, qis, k = case
    groups, count = partition(frame.to_numpy(dtype=np.float64), qis, k)[:2]
    sizes = np.bincount(groups, minlength=count)
    assert sizes.sum() == len(frame)
    if len(frame) == 0:
        assert count == 0
        return
    assert sizes.min() >= k
    assert sizes.max() <= 2 * k - 1
    assert (sizes[:-1] == k).all()


@PROPERTY_SETTINGS
@given(cases())
def test_k_anonymous_totals_kept_and_report_consistent(
    case: tuple[pd.DataFrame, list[str], int],
) -> None:
    frame, qis, k = case
    result = microaggregate(frame, qis, k=k)
    out = result.table
    report = result.report
    if len(frame):
        class_sizes = out.groupby(qis, sort=False).size()
        assert class_sizes.min() >= k
    for name in qis:
        before = frame[name].to_numpy(dtype=np.float64)
        after = out[name].to_numpy()
        tolerance = 1e-9 * (1.0 + np.abs(before).sum())
        assert abs(after.sum() - before.sum()) <= tolerance
        if (before == 0.1).all() and len(before):
            assert (after == 0.1).all()
    assert 0.0 <= report.information_loss <= 1.0
    assert report.sse <= report.sst * (1 + 1e-12) + 1e-12
    non_constant = sum(
        1 for name in qis if len(frame) and not (frame[name] == frame[name].iloc[0]).all()
    )
    assert report.sst == pytest.approx(len(frame) * non_constant, rel=1e-9, abs=1e-12)
    if report.sst:
        assert report.information_loss == pytest.approx(report.sse / report.sst, rel=1e-12)
    assert (
        sum(
            report.group_size_distribution[i][1] for i in range(len(report.group_size_distribution))
        )
        == report.group_count
    )


@PROPERTY_SETTINGS
@given(cases())
def test_deterministic(case: tuple[pd.DataFrame, list[str], int]) -> None:
    frame, qis, k = case
    first = microaggregate(frame, qis, k=k)
    second = microaggregate(frame, qis, k=k)
    for name in qis:
        assert first.table[name].to_numpy().tobytes() == second.table[name].to_numpy().tobytes()
    assert first.report == second.report


_SCRIPT = """
import hashlib, numpy as np, pandas as pd
from decoy_engine.anonymize.microaggregation import microaggregate
x = np.random.default_rng(5).normal(size=(500, 3))
r = microaggregate(pd.DataFrame(x, columns=list("abc")), list("abc"), k=4)
print(hashlib.sha256(r.table.to_numpy().tobytes()).hexdigest(), repr(r.report.sse))
"""


def test_deterministic_across_processes() -> None:
    runs = {
        subprocess.run(  # noqa: S603 -- args are test literals
            [sys.executable, "-c", _SCRIPT], capture_output=True, text=True, check=True
        ).stdout
        for _ in range(2)
    }
    assert len(runs) == 1


@pytest.mark.parametrize("seed", range(20))
def test_row_order_does_not_matter_on_continuous_data(seed: int) -> None:
    # Continuous data has no exact distance ties. Shuffling changes summation order,
    # so output bits may differ in the last place; the partition must not.
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(60, 3))
    order = rng.permutation(60)
    names = ["a", "b", "c"]
    groups = partition(x, names, 3)[0]
    shuffled_groups = partition(x[order], names, 3)[0]

    def blocks(g: np.ndarray, ids: np.ndarray) -> set[frozenset[int]]:
        return {frozenset(ids[g == i].tolist()) for i in np.unique(g)}

    assert blocks(groups, np.arange(60)) == blocks(shuffled_groups, order)
    out = microaggregate(pd.DataFrame(x, columns=names), names, k=3).table.to_numpy()
    out_shuffled = microaggregate(
        pd.DataFrame(x[order], columns=names), names, k=3
    ).table.to_numpy()
    np.testing.assert_allclose(out_shuffled, out[order], rtol=1e-12, atol=1e-15)
