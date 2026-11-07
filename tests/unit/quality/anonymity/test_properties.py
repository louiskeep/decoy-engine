"""Property tests: the measures equal independent reference computations.

The references here are written from the papers' formulas directly (dense,
exact ``Fraction`` arithmetic, a pure-Python ``Counter`` with its own type
family table), so they share no code with the module under test.
"""

from __future__ import annotations

import datetime
import math
from collections import Counter
from decimal import Decimal
from fractions import Fraction
from typing import Any

import numpy as np
import pandas as pd
from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st

from decoy_engine.quality.anonymity import (
    measure_k_anonymity,
    measure_l_diversity,
    measure_t_closeness_equal,
    measure_t_closeness_ordered,
)

TOL = 1e-12
_NULL = object()


def _is_null(v: Any) -> bool:
    if v is None or v is pd.NA or v is pd.NaT:
        return True
    return isinstance(v, float) and math.isnan(v)


def _family(v: Any) -> str:
    # Independent restatement of the documented family table.
    if isinstance(v, (bool, np.bool_)):
        return "bool"
    if isinstance(v, (int, np.integer)):
        return "int"
    if isinstance(v, (float, np.floating)):
        return "float"
    if isinstance(v, Decimal):
        return "decimal"
    if isinstance(v, str):
        return "str"
    if isinstance(v, (datetime.datetime, np.datetime64)):
        return "datetime"
    return type(v).__qualname__


def _qi_key(row: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(_NULL if _is_null(v) else (_family(v), v) for v in row)


def _classes(frame: pd.DataFrame, qi: list[str]) -> dict[tuple[Any, ...], list[int]]:
    groups: dict[tuple[Any, ...], list[int]] = {}
    for i, row in enumerate(frame[qi].itertuples(index=False, name=None)):
        groups.setdefault(_qi_key(row), []).append(i)
    return groups


def _dense_emds(
    frame: pd.DataFrame, qi: list[str], sensitive: str, ordered: bool
) -> list[Fraction]:
    values = [v for v in frame[sensitive] if not _is_null(v)]
    domain = sorted(set(values))
    m = len(domain)
    total = Counter(values)
    n = len(values)
    q = [Fraction(total[v], n) for v in domain]
    emds = []
    for rows in _classes(frame, qi).values():
        cls = [frame[sensitive].iloc[i] for i in rows]
        cls = [v for v in cls if not _is_null(v)]
        if not cls:
            continue
        counts = Counter(cls)
        p = [Fraction(counts[v], len(cls)) for v in domain]
        if ordered:
            if m == 1:
                emds.append(Fraction(0))
                continue
            acc = Fraction(0)
            run = Fraction(0)
            for pi, qi_ in zip(p, q, strict=True):
                run += pi - qi_
                acc += abs(run)
            emds.append(acc / (m - 1))
        else:
            emds.append(sum((abs(pi - qi_) for pi, qi_ in zip(p, q, strict=True)), Fraction(0)) / 2)
    return emds


def _close(actual: float, expected: Fraction) -> bool:
    return math.isclose(actual, float(expected), rel_tol=TOL, abs_tol=TOL)


_qi_cell = st.one_of(
    st.none(),
    st.integers(-2, 2),
    st.integers(-2, 2).map(np.int64),
    st.sampled_from([0.0, 1.0, 2.0, float("nan")]),
    st.booleans(),
    st.sampled_from(["a", "b"]),
    st.sampled_from(["a", "b"]).map(np.str_),
)


@st.composite
def _mixed_frames(draw: Any) -> pd.DataFrame:
    n = draw(st.integers(0, 25))
    a = draw(st.lists(_qi_cell, min_size=n, max_size=n))
    b = draw(st.lists(st.sampled_from(["x", "y", None]), min_size=n, max_size=n))
    s = draw(st.lists(st.one_of(st.none(), st.integers(0, 6)), min_size=n, max_size=n))
    return pd.DataFrame(
        {
            "a": pd.Series(a, dtype=object),
            "b": pd.Series(b, dtype=object),
            "s": pd.Series([np.nan if v is None else float(v) for v in s], dtype=float),
        }
    )


_SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@seed(20261107)
@_SETTINGS
@given(_mixed_frames())
def test_models_agree_on_classes(frame: pd.DataFrame) -> None:
    qi = ["a", "b"]
    expected = _classes(frame, qi)
    sizes = Counter(len(rows) for rows in expected.values())
    k = measure_k_anonymity(frame, qi)
    assert k.row_count == len(frame)
    assert k.class_count == len(expected)
    assert k.class_size_distribution == tuple(sorted(sizes.items()))
    assert k.k_min == (min(sizes) if sizes else None)
    for report in (
        measure_l_diversity(frame, qi, "s"),
        measure_t_closeness_equal(frame, qi, "s"),
        measure_t_closeness_ordered(frame, qi, "s"),
    ):
        assert report.row_count == k.row_count
        assert report.class_count == k.class_count


@seed(20261107)
@_SETTINGS
@given(_mixed_frames())
def test_emd_equals_dense_reference(frame: pd.DataFrame) -> None:
    qi = ["a", "b"]
    for ordered, measure in (
        (False, measure_t_closeness_equal),
        (True, measure_t_closeness_ordered),
    ):
        emds = _dense_emds(frame, qi, "s", ordered)
        report = measure(frame, qi, "s")
        if not emds:
            assert report.max_emd is None
            continue
        assert _close(report.max_emd, max(emds))
        assert 0.0 <= report.max_emd <= 1.0 + TOL
        # Every class is pinned by thresholds on either side of its value.
        for value in set(emds):
            for t in (float(value) + 1e-9, float(value) - 1e-9):
                t = min(1.0, max(0.0, t))
                above = sum(1 for e in emds if float(e) > t + TOL)
                assert measure(frame, qi, "s", t_target=t).classes_above_target == above


@seed(20261107)
@_SETTINGS
@given(_mixed_frames(), st.floats(0.5, 4.0))
def test_l_diversity_reference_and_bounds(frame: pd.DataFrame, c: float) -> None:
    qi = ["a", "b"]
    report = measure_l_diversity(frame, qi, "s", recursive_c=c)
    distinct, entropy_l, recursive = [], [], []
    for rows in _classes(frame, qi).values():
        cls = [frame["s"].iloc[i] for i in rows]
        cls = [v for v in cls if not _is_null(v)]
        counts = sorted(Counter(cls).values(), reverse=True)
        distinct.append(len(counts))
        if not counts:
            entropy_l.append(0.0)
            recursive.append(0)
            continue
        n = sum(counts)
        entropy_l.append(math.exp(-sum((r / n) * math.log(r / n) for r in counts)))
        best = 0
        for level in range(1, len(counts) + 1):
            if counts[0] < c * sum(counts[level - 1 :]):
                best = level
        recursive.append(best)
    if not distinct:
        assert report.distinct_l_min == 0
        return
    assert report.distinct_l_min == min(distinct)
    assert math.isclose(report.entropy_l_min, min(entropy_l), rel_tol=1e-9, abs_tol=1e-12)
    assert report.recursive_l_min == min(recursive)
    assert report.recursive_l_min <= report.distinct_l_min
    if report.classes_without_sensitive_values == 0:
        assert 1.0 - 1e-12 <= report.entropy_l_min <= report.distinct_l_min + 1e-9


@seed(20261107)
@_SETTINGS
@given(_mixed_frames(), st.randoms(use_true_random=False))
def test_row_order_does_not_matter(frame: pd.DataFrame, rnd: Any) -> None:
    order = list(range(len(frame)))
    rnd.shuffle(order)
    shuffled = frame.iloc[order].reset_index(drop=True)
    qi = ["a", "b"]
    assert measure_k_anonymity(frame, qi, k_target=2) == measure_k_anonymity(
        shuffled, qi, k_target=2
    )
    assert measure_l_diversity(frame, qi, "s", l_target=2, recursive_c=2) == measure_l_diversity(
        shuffled, qi, "s", l_target=2, recursive_c=2
    )
    for measure in (measure_t_closeness_equal, measure_t_closeness_ordered):
        a = measure(frame, qi, "s", t_target=0.25)
        b = measure(shuffled, qi, "s", t_target=0.25)
        assert a.classes_above_target == b.classes_above_target
        if a.max_emd is None:
            assert b.max_emd is None
        else:
            assert math.isclose(a.max_emd, b.max_emd, rel_tol=TOL, abs_tol=TOL)


def test_segment_split_where_ceiling_is_hit_exactly() -> None:
    """Pinned: the split position s is the first CQ >= ceil(CP*N / n_c). Class A
    = {0, 2, 2}, B = {1}; N = 4, CQ = (1, 2, 4). In A's first segment CP = 1,
    ceil(4/3) = 2 = CQ(1), and CQ(1)*n_c = 6 > CP*N = 4, so position 1 is on the
    upper side. A: cumulative r = 1/12, -1/6, 0 -> 1/4 / 2 = 1/8.
    B: -1/4, 1/2, 0 -> 3/4 / 2 = 3/8."""
    frame = pd.DataFrame({"q": ["A", "B", "A", "A"], "s": [0, 1, 2, 2]})
    for t, above in ((3 / 8, 0), (3 / 8 - 1e-9, 1), (1 / 8, 1), (1 / 8 - 1e-9, 2)):
        report = measure_t_closeness_ordered(frame, ["q"], "s", t_target=t)
        assert report.classes_above_target == above, t


def test_python_int_path_equals_int64_path(monkeypatch: Any) -> None:
    """Above the int64 bound the arithmetic switches to Python ints; forcing
    that path on ordinary inputs must give the same numbers."""
    import decoy_engine.quality.anonymity._t_closeness as module

    frames = [
        pd.DataFrame({"q": ["A", "B", "A", "A"], "s": [0, 1, 2, 2]}),
        pd.DataFrame({"q": list("aabbbc"), "s": [5, 1, 1, 3, 9, 9]}),
    ]
    expected = [
        (measure_t_closeness_ordered(f, ["q"], "s"), measure_t_closeness_equal(f, ["q"], "s"))
        for f in frames
    ]
    monkeypatch.setattr(module, "_INT64_SAFE", 0)
    for f, (ordered, equal) in zip(frames, expected, strict=True):
        assert measure_t_closeness_ordered(f, ["q"], "s") == ordered
        assert measure_t_closeness_equal(f, ["q"], "s") == equal


def test_exact_numerator_at_scale() -> None:
    """Correctness pin at scale: n_c = 1 against N = m = 200001, where the
    segment terms reach about 4e15. Every term stays below 2**53, so this does
    not distinguish exact integers from float64 (an equivalent mutant at this
    size); it checks the sparse evaluation on a large, lopsided table.
    One class holds value 0 alone; the rest share one class."""
    n = 200_001
    s = np.arange(n, dtype=np.int64)
    q = np.where(s == 0, "solo", "rest")
    frame = pd.DataFrame({"q": q, "s": s})
    report = measure_t_closeness_ordered(frame, ["q"], "s")
    # Solo class: F = 1 from position 0; G(i) = (i+1)/N. Sum over i = 0..m-2 of
    # (1 - (i+1)/N) = (m-1) - (m-1)m/(2N); divided by (m-1): 1 - m/(2N).
    expected = 1 - Fraction(n, 2 * n)
    assert _close(report.max_emd, expected)
