"""Unit tests for graph/cache.py."""

import pyarrow as pa
import pandas as pd
import pytest

from decoy_engine.graph.cache import GraphCache


def _df(n: int) -> pd.DataFrame:
    return pd.DataFrame({"x": range(n)})


# ---------------------------------------------------------------------------
# write()

class TestWrite:
    def test_returns_row_count(self):
        gc = GraphCache({"a": 1})
        assert gc.write("a", _df(5), "pandas") == 5

    def test_evicts_on_zero_consumers(self):
        gc = GraphCache({"a": 0})
        gc.write("a", _df(3), "pandas")
        assert gc.get_arrow("a") is None

    def test_keeps_pinned_zero_consumer(self):
        gc = GraphCache({"a": 0}, keep_keys={"a"})
        gc.write("a", _df(3), "pandas")
        assert gc.get_arrow("a") is not None

    def test_row_limit_truncates(self):
        gc = GraphCache({"a": 1})
        gc.write("a", _df(100), "pandas", row_limit=10)
        assert gc.get_arrow("a").num_rows == 10

    def test_row_limit_no_op_when_under(self):
        gc = GraphCache({"a": 1})
        gc.write("a", _df(5), "pandas", row_limit=10)
        assert gc.get_arrow("a").num_rows == 5

    def test_none_value_stores_none(self):
        gc = GraphCache({"a": 1})
        gc.write("a", None, "pandas")
        assert gc.get_arrow("a") is None


# ---------------------------------------------------------------------------
# write_split()

class TestWriteSplit:
    def test_returns_total_rows(self):
        gc = GraphCache({"a.pass": 1, "a.reject": 1})
        result = {"pass": _df(7), "reject": _df(3)}
        assert gc.write_split("a", ("pass", "reject"), result, "pandas") == 10

    def test_stores_port_keys(self):
        gc = GraphCache({"a.pass": 1, "a.reject": 1})
        gc.write_split("a", ("pass", "reject"), {"pass": _df(2), "reject": _df(1)}, "pandas")
        assert gc.get_arrow("a.pass").num_rows == 2
        assert gc.get_arrow("a.reject").num_rows == 1

    def test_evicts_zero_consumer_port(self):
        gc = GraphCache({"a.pass": 0, "a.reject": 1})
        gc.write_split("a", ("pass", "reject"), {"pass": _df(2), "reject": _df(1)}, "pandas")
        assert gc.get_arrow("a.pass") is None
        assert gc.get_arrow("a.reject") is not None

    def test_row_limit_per_port(self):
        gc = GraphCache({"a.pass": 1})
        gc.write_split("a", ("pass",), {"pass": _df(50)}, "pandas", row_limit=5)
        assert gc.get_arrow("a.pass").num_rows == 5


# ---------------------------------------------------------------------------
# read()

class TestRead:
    def test_decrements_and_evicts_at_zero(self):
        gc = GraphCache({"a": 1})
        gc.write("a", _df(3), "pandas")
        gc.read("a", "pandas")
        assert gc.get_arrow("a") is None

    def test_does_not_evict_with_remaining_consumers(self):
        gc = GraphCache({"a": 2})
        gc.write("a", _df(3), "pandas")
        gc.read("a", "pandas")
        assert gc.get_arrow("a") is not None

    def test_does_not_evict_pinned_key(self):
        # keep_keys adds +1 slot, so natural(1) + pin(1) = 2 total.
        # One read brings it to 1 — still present.
        gc = GraphCache({"a": 1}, keep_keys={"a"})
        gc.write("a", _df(3), "pandas")
        gc.read("a", "pandas")
        assert gc.get_arrow("a") is not None

    def test_missing_key_returns_none(self):
        gc = GraphCache({})
        assert gc.read("x", "pandas") is None

    def test_returns_correct_value(self):
        gc = GraphCache({"a": 1})
        gc.write("a", _df(3), "pandas")
        val = gc.read("a", "pandas")
        assert hasattr(val, "shape")  # pandas DataFrame
        assert len(val) == 3


# ---------------------------------------------------------------------------
# peek_rows()

class TestPeekRows:
    def test_no_consume(self):
        gc = GraphCache({"a": 2})
        gc.write("a", _df(7), "pandas")
        assert gc.peek_rows("a") == 7
        assert gc.peek_rows("a") == 7  # idempotent

    def test_missing_returns_zero(self):
        gc = GraphCache({})
        assert gc.peek_rows("x") == 0


# ---------------------------------------------------------------------------
# collect_kept()

class TestCollectKept:
    def test_returns_pinned_entries(self):
        gc = GraphCache({"a": 1}, keep_keys={"a"})
        gc.write("a", _df(4), "pandas")
        gc.read("a", "pandas")  # decrement once; pin holds it
        kept = gc.collect_kept()
        assert "a" in kept
        assert isinstance(kept["a"], pa.Table)

    def test_excludes_unwritten_pinned_key(self):
        gc = GraphCache({"a": 0}, keep_keys={"b"})
        gc.write("a", _df(2), "pandas")
        kept = gc.collect_kept()
        assert "b" not in kept
        assert "a" not in kept

    def test_multiple_keys(self):
        gc = GraphCache({"a": 0, "b": 0}, keep_keys={"a", "b"})
        gc.write("a", _df(2), "pandas")
        gc.write("b", _df(3), "pandas")
        assert set(gc.collect_kept()) == {"a", "b"}


# ---------------------------------------------------------------------------
# store_arrow()

class TestStoreArrow:
    def test_aliases_split_pass_port(self):
        gc = GraphCache({"a.pass": 1}, keep_keys={"a"})
        gc.write_split("a", ("pass",), {"pass": _df(5)}, "pandas")
        gc.store_arrow("a", gc.get_arrow("a.pass"))
        assert gc.get_arrow("a").num_rows == 5

    def test_stores_none(self):
        gc = GraphCache({"a": 0}, keep_keys={"a"})
        gc.store_arrow("a", None)
        assert gc.get_arrow("a") is None


# ---------------------------------------------------------------------------
# keep-key pinning contract

class TestKeepKeyPinning:
    def test_zero_consumer_kept_key_survives_write(self):
        gc = GraphCache({"out": 0}, keep_keys={"out"})
        gc.write("out", _df(5), "pandas")
        assert gc.get_arrow("out") is not None

    def test_zero_consumer_non_kept_evicts_on_write(self):
        gc = GraphCache({"out": 0})
        gc.write("out", _df(5), "pandas")
        assert gc.get_arrow("out") is None
