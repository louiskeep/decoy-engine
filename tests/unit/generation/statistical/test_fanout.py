"""Allocation, decoder and assignment for the fitted fan-out (pure, no I/O)."""

from __future__ import annotations

import random
from collections import Counter
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine import reasons
from decoy_engine.generation.statistical import StatisticalSpecError
from decoy_engine.generation.statistical._fanout import (
    FanoutSpec,
    allocate_degrees,
    fanout_spec_from_snapshot,
    fanout_stats,
    fitted_reference_values,
)

DIGEST = "ab" * 32

QA_DEGREES = ((1, 5), (2, 1), (5, 1), (8, 1))


def _spec(degrees: tuple[tuple[int, int], ...] = QA_DEGREES) -> FanoutSpec:
    return FanoutSpec(
        rows=sum(d * c for d, c in degrees),
        keys=sum(c for _, c in degrees),
        degrees=degrees,
        digest=DIGEST,
    )


@st.composite
def _specs(draw: st.DrawFn) -> FanoutSpec:
    n = draw(st.integers(1, 40))
    degs = sorted(draw(st.sets(st.integers(1, 10_000), min_size=n, max_size=n)))
    counts = [draw(st.integers(1, 25)) for _ in degs]
    return _spec(tuple(zip(degs, counts, strict=True)))


def _expected_k(spec: FanoutSpec, p: int, n: int) -> int:
    return min(p, n, max(1, round(Fraction(n * spec.keys, spec.rows))))


@settings(max_examples=300, deadline=None)
@given(spec=_specs(), p=st.integers(0, 5000), n=st.integers(0, 50_000))
def test_allocation_properties(spec: FanoutSpec, p: int, n: int) -> None:
    out = allocate_degrees(spec, p, n)
    assert out == allocate_degrees(spec, p, n)
    if p == 0 or n == 0:
        assert out == []
        return
    k = _expected_k(spec, p, n)
    assert len(out) == k <= min(p, n)
    assert all(d >= 1 for d in out)
    assert sum(out) == n
    dmax = spec.degrees[-1][0]
    if n <= k * dmax:
        assert max(out) <= dmax


@settings(max_examples=100, deadline=None)
@given(spec=_specs(), extra=st.integers(0, 50))
def test_same_row_count_returns_source_multiset(spec: FanoutSpec, extra: int) -> None:
    out = allocate_degrees(spec, spec.keys + extra, spec.rows)
    source = Counter({d: c for d, c in spec.degrees})
    assert len(out) == spec.keys
    assert Counter(out) == source


def test_qa_repro_same_counts_is_the_source() -> None:
    assert allocate_degrees(_spec(), 10, 20) == [1, 1, 1, 1, 1, 2, 5, 8]


def test_qa_repro_scaled_up() -> None:
    out = allocate_degrees(_spec(), 30, 60)
    assert Counter(out) == Counter({1: 15, 2: 3, 5: 3, 8: 3})


def test_fixed_rows() -> None:
    spec = _spec()
    assert allocate_degrees(spec, 10, 0) == []
    assert allocate_degrees(spec, 0, 0) == []
    out = allocate_degrees(spec, 10, 3)  # fewer child rows than source keys
    assert len(out) <= 3 and sum(out) == 3 and min(out) >= 1
    out = allocate_degrees(spec, 4, 1000)  # K clamped to P
    assert len(out) == 4 and sum(out) == 1000
    one = _spec(((7, 1),))
    assert allocate_degrees(one, 5, 21) == [7, 7, 7]


def test_half_even_k() -> None:
    # 20 rows / 8 keys; N=5 gives 5*8/20 = 2.0; N=10 gives 4; N=3 gives 1.2 -> 1
    spec = _spec()
    assert len(allocate_degrees(spec, 100, 5)) == 2
    # a tie: keys=1 rows=2 (one parent with 2 children); N=3 -> 1.5 -> half even 2
    tie = _spec(((2, 1),))
    assert len(allocate_degrees(tie, 100, 3)) == 2
    # N=5 -> 2.5 -> half even 2 (not 3)
    assert len(allocate_degrees(tie, 100, 5)) == 2


def test_cap_holds_below_source_max() -> None:
    spec = _spec()
    out = allocate_degrees(spec, 8, 20)
    assert max(out) <= 8 and sum(out) == 20


def test_values_use_only_pool_and_exact_count() -> None:
    pool = [f"k{i}" for i in range(30)]
    out = fitted_reference_values(pool, _spec(), 60, random.Random(7))
    assert len(out) == 60
    assert set(out) <= set(pool)
    counts = Counter(out)
    assert max(counts.values()) == 8
    assert len(counts) == 24


def test_values_deterministic_and_seed_sensitive() -> None:
    pool = [f"k{i}" for i in range(30)]
    a = fitted_reference_values(pool, _spec(), 60, random.Random(7))
    b = fitted_reference_values(pool, _spec(), 60, random.Random(7))
    c = fitted_reference_values(pool, _spec(), 60, random.Random(8))
    assert a == b
    assert a != c


def test_zero_rows_is_empty_and_empty_pool_refuses() -> None:
    assert fitted_reference_values([], _spec(), 0, random.Random(1)) == []
    with pytest.raises(StatisticalSpecError) as exc:
        fitted_reference_values([], _spec(), 5, random.Random(1))
    assert exc.value.code == reasons.plan.REFERENCE_PARENT_POOL_EMPTY


def test_seed_digest_has_prefix() -> None:
    assert _spec().seed_digest == f"sha256:{DIGEST}"


# ── decoder ──────────────────────────────────────────────────────────────────


def _snap(block: object, col: str = "patient_id") -> dict:
    return {"columns": {col: {"kind": "categorical", "fanout": block}}}


GOOD = {"rows": 20, "keys": 8, "degrees": [[1, 5], [2, 1], [5, 1], [8, 1]]}


def test_decoder_roundtrip_from_writer() -> None:
    import pandas as pd

    s = pd.Series(["a"] * 3 + ["b"] + [None, "c", "c"])
    block = fanout_stats(s)
    assert block == {"rows": 6, "keys": 3, "degrees": [[1, 1], [2, 1], [3, 1]]}
    spec = fanout_spec_from_snapshot(_snap(block), "patient_id", digest=DIGEST)
    assert (spec.rows, spec.keys, spec.degrees) == (6, 3, ((1, 1), (2, 1), (3, 1)))
    assert spec.digest == DIGEST


def test_decoder_missing() -> None:
    for snap in ({"columns": {}}, {"columns": {"patient_id": {"kind": "x"}}}, {}):
        with pytest.raises(StatisticalSpecError) as exc:
            fanout_spec_from_snapshot(snap, "patient_id", digest=DIGEST)
        assert exc.value.code == reasons.plan.REFERENCE_FANOUT_MISSING


CANARY = 987654321


@pytest.mark.parametrize(
    "block",
    [
        "text",
        [],
        {"rows": 20, "keys": 8},
        {"rows": 20, "keys": 8, "degrees": "x"},
        {"rows": 20, "keys": 8, "degrees": ((1, 5), (2, 1), (5, 1), (8, 1))},
        {"rows": 20, "keys": 8, "degrees": [(1, 5), [2, 1], [5, 1], [8, 1]]},
        {"rows": 21, "keys": 8, "degrees": GOOD["degrees"]},  # rows total off
        {"rows": 20, "keys": 9, "degrees": GOOD["degrees"]},  # keys total off
        {"rows": 20, "keys": 8, "degrees": [[0, 5], [2, 1], [5, 1], [8, 1]]},
        {"rows": 20, "keys": 8, "degrees": [[1, 0], [2, 6], [5, 1], [8, 1]]},
        {"rows": 20, "keys": 8, "degrees": [[2, 1], [1, 5], [5, 1], [8, 1]]},  # unsorted
        {"rows": 20, "keys": 8, "degrees": [[1, 5], [1, 1], [5, 1], [8, 1]]},  # duplicate
        {"rows": 20, "keys": 8, "degrees": [[1, 5], [2, 1], [5, 1], [8]]},
        {"rows": 20, "keys": 8, "degrees": [[1, 5], [2, 1], [5, 1], [8, 1, 1]]},
        {"rows": 0, "keys": 0, "degrees": []},
        {"rows": -20, "keys": 8, "degrees": GOOD["degrees"]},
        {"rows": True, "keys": 8, "degrees": GOOD["degrees"]},
        {"rows": 20.0, "keys": 8, "degrees": GOOD["degrees"]},
        {"rows": "20", "keys": 8, "degrees": GOOD["degrees"]},
        {"rows": 20, "keys": 8, "degrees": [[1, 5], [2, 1], [5, 1], [8.0, 1]]},
        {"rows": 20, "keys": 8, "degrees": [[1, 5], [2, 1], [5, 1], [8, True]]},
        {"rows": 20, "keys": 8, "degrees": [[1, 5], [2, 1], [5, 1], ["8", 1]]},
    ],
)
def test_decoder_refuses_malformed(block: object) -> None:
    with pytest.raises(StatisticalSpecError) as exc:
        fanout_spec_from_snapshot(_snap(block), "patient_id", digest=DIGEST)
    assert exc.value.code == reasons.plan.REFERENCE_FANOUT_INVALID


def test_decoder_messages_never_echo_values() -> None:
    bad = [
        {"rows": CANARY, "keys": 8, "degrees": GOOD["degrees"]},
        {"rows": "canary-text", "keys": 8, "degrees": GOOD["degrees"]},
        {"rows": 20, "keys": 8, "degrees": [[1, 5], [2, 1], [5, 1], [CANARY, True]]},
        {"rows": 20, "keys": 8, "degrees": [[1, 5], [2, 1], [5, 1], ["canary-text", 1]]},
    ]
    for block in bad:
        with pytest.raises(StatisticalSpecError) as exc:
            fanout_spec_from_snapshot(_snap(block), "patient_id", digest=DIGEST)
        text = str(exc.value)
        assert str(CANARY) not in text and "canary-text" not in text
