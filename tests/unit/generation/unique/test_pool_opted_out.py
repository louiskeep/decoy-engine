"""GEN-UNIQUE E1: `pool_opted_out` is the one reader of the column's `pooled` key."""

from __future__ import annotations

import pytest

from decoy_engine.generation import _faker_pool
from decoy_engine.generation._faker_pool import pool_eligible, pool_opted_out


@pytest.mark.parametrize(
    ("col", "expected"),
    [
        ({"pooled": False}, True),
        ({"pooled": True}, False),
        ({"pooled": None}, False),
        ({}, False),
        ({"pooled": 0}, False),
        ({"pooled": "false"}, False),
    ],
)
def test_only_an_explicit_false_opts_out(col, expected):
    assert pool_opted_out(col) is expected


def test_admission_follows_the_predicate():
    n = _faker_pool.N_THRESHOLD
    assert pool_eligible("first_name", None, n, opted_out=pool_opted_out({})) is True
    assert (
        pool_eligible("first_name", None, n, opted_out=pool_opted_out({"pooled": False})) is False
    )


def test_try_pool_honours_the_opt_out(monkeypatch):
    calls = []
    monkeypatch.setattr(_faker_pool, "build_and_sample", lambda **k: calls.append(k) or ["x"])
    col = {"name": "c", "type": "faker", "faker_type": "first_name", "pooled": False}
    n = _faker_pool.N_THRESHOLD
    assert _faker_pool.try_pool(col, n, 1, None, None, {}) is None
    assert calls == []


def test_try_pool_pools_when_not_opted_out(monkeypatch):
    calls = []
    monkeypatch.setattr(_faker_pool, "build_and_sample", lambda **k: calls.append(k) or ["x"])
    col = {"name": "c", "type": "faker", "faker_type": "first_name", "_table": "t"}
    n = _faker_pool.N_THRESHOLD
    assert _faker_pool.try_pool(col, n, 1, None, None, {}) == ["x"]
    assert len(calls) == 1
