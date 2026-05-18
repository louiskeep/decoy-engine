"""Unit tests for graph.cache (Sprint 1.2)."""
import pyarrow as pa
import pytest

from decoy_engine.graph.cache import GraphCache


def _tbl(n_rows: int = 3) -> pa.Table:
    return pa.table({"x": list(range(n_rows))})


# ── store_from_op ──────────────────────────────────────────────────────────

def test_store_from_op_returns_arrow_table():
    cache = GraphCache({"a": 1})
    import pandas as pd
    df = pd.DataFrame({"x": [1, 2, 3]})
    result = cache.store_from_op("a", df, "pandas")
    assert isinstance(result, pa.Table)
    assert result.num_rows == 3


def test_store_from_op_none_result():
    cache = GraphCache({"a": 1})
    result = cache.store_from_op("a", None, "pandas")
    assert result is None


def test_store_from_op_row_limit_caps_rows():
    cache = GraphCache({"a": 1})
    import pandas as pd
    df = pd.DataFrame({"x": list(range(100))})
    result = cache.store_from_op("a", df, "pandas", row_limit=10)
    assert result is not None
    assert result.num_rows == 10


def test_store_from_op_row_limit_none_keeps_all():
    cache = GraphCache({"a": 1})
    import pandas as pd
    df = pd.DataFrame({"x": list(range(50))})
    result = cache.store_from_op("a", df, "pandas", row_limit=None)
    assert result is not None
    assert result.num_rows == 50


# ── eviction ──────────────────────────────────────────────────────────────────

def test_evict_immediately_when_zero_consumers():
    cache = GraphCache({"sink": 0})
    cache.store_from_op("sink", pa.table({"x": [1]}), "pandas")
    assert cache.get_arrow("sink") is None


def test_keep_entry_when_consumers_remain():
    cache = GraphCache({"a": 2})
    t = _tbl()
    cache.store_from_op("a", t, "pandas")
    assert cache.get_arrow("a") is not None


def test_evict_after_last_consume():
    cache = GraphCache({"a": 1})
    cache.store_from_op("a", _tbl(), "pandas")
    cache.consume("a", "pandas")
    assert cache.get_arrow("a") is None


def test_keep_entry_after_partial_consume():
    cache = GraphCache({"a": 2})
    cache.store_from_op("a", _tbl(), "pandas")
    cache.consume("a", "pandas")
    assert cache.get_arrow("a") is not None


def test_consume_absent_key_returns_none():
    cache = GraphCache({})
    assert cache.consume("ghost", "pandas") is None


# ── keep_set ───────────────────────────────────────────────────────────────────

def test_keep_set_pins_entry_despite_zero_consumers():
    cache = GraphCache({"a": 0}, keep_set={"a"})
    cache.store_from_op("a", _tbl(), "pandas")
    assert cache.get_arrow("a") is not None


def test_keep_set_pins_entry_after_all_consumed():
    cache = GraphCache({"a": 1}, keep_set={"a"})
    cache.store_from_op("a", _tbl(), "pandas")
    cache.consume("a", "pandas")
    assert cache.get_arrow("a") is not None


def test_snapshot_returns_keep_set_entries():
    t = _tbl(5)
    cache = GraphCache({"a": 0, "b": 0}, keep_set={"a"})
    cache.store_from_op("a", t, "pandas")
    cache.store_from_op("b", _tbl(), "pandas")
    snap = cache.snapshot()
    assert "a" in snap
    assert "b" not in snap


def test_snapshot_excludes_evicted_entries():
    cache = GraphCache({"a": 0}, keep_set={"a"})
    # never stored, so not in tables
    snap = cache.snapshot()
    assert "a" not in snap


# ── hold in consume ──────────────────────────────────────────────────────────

def test_hold_prevents_eviction_at_last_consume():
    cache = GraphCache({"tgt": 1})
    cache.store_from_op("tgt", _tbl(), "pandas")
    # consume it, but hold="tgt" prevents eviction
    cache.consume("tgt", "pandas", hold="tgt")
    assert cache.get_arrow("tgt") is not None


def test_hold_does_not_prevent_eviction_of_other_key():
    cache = GraphCache({"a": 1, "tgt": 1})
    cache.store_from_op("a", _tbl(), "pandas")
    cache.store_from_op("tgt", _tbl(), "pandas")
    cache.consume("a", "pandas", hold="tgt")
    assert cache.get_arrow("a") is None
    assert cache.get_arrow("tgt") is not None


# ── row_count ───────────────────────────────────────────────────────────────────

def test_row_count_returns_zero_for_absent_key():
    assert GraphCache({}).row_count("ghost") == 0


def test_row_count_matches_stored_table():
    cache = GraphCache({"a": 1})
    cache.store_from_op("a", _tbl(7), "pandas")
    assert cache.row_count("a") == 7
