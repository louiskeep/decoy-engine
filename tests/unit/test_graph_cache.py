"""Unit tests for graph.cache.GraphCache."""

import pandas as pd
import pyarrow as pa

from decoy_engine.graph.cache import GraphCache


def _df(n: int) -> pd.DataFrame:
    return pd.DataFrame({"v": list(range(n))})


def _tbl(n: int) -> pa.Table:
    return pa.table({"v": list(range(n))})


class TestStreamEviction:
    def test_evicts_at_last_consumer(self):
        cache = GraphCache({"a": 1})
        cache.write_stream("a", _df(3), "pandas")
        assert cache.get("a") is not None
        cache.consume("a", "pandas")
        assert cache.get("a") is None

    def test_multi_consumer_evicts_at_last(self):
        cache = GraphCache({"a": 2})
        cache.write_stream("a", _df(3), "pandas")
        cache.consume("a", "pandas")
        assert cache.get("a") is not None
        cache.consume("a", "pandas")
        assert cache.get("a") is None

    def test_sink_evicted_immediately_on_write(self):
        cache = GraphCache({"sink": 0})
        cache.write_stream("sink", _df(5), "pandas")
        assert cache.get("sink") is None

    def test_write_none_result_stores_none(self):
        cache = GraphCache({"a": 1})
        cache.write_stream("a", None, "pandas")
        assert cache.get("a") is None


class TestKeepSet:
    def test_keep_prevents_eviction_after_consume(self):
        cache = GraphCache({"a": 1}, keep={"a"})
        cache.write_stream("a", _df(3), "pandas")
        cache.consume("a", "pandas")
        assert cache.get("a") is not None

    def test_keep_prevents_immediate_eviction_on_zero_consumers(self):
        cache = GraphCache({"a": 0}, keep={"a"})
        cache.write_stream("a", _df(3), "pandas")
        assert cache.get("a") is not None

    def test_kept_returns_only_kept_entries(self):
        cache = GraphCache({"a": 1, "b": 0}, keep={"b"})
        cache.write_stream("a", _df(2), "pandas")
        cache.write_stream("b", _df(4), "pandas")
        kept = cache.kept()
        assert "b" in kept
        assert "a" not in kept
        assert kept["b"].num_rows == 4

    def test_kept_empty_when_no_keep(self):
        cache = GraphCache({"a": 1})
        cache.write_stream("a", _df(2), "pandas")
        assert cache.kept() == {}


class TestRowSum:
    def test_row_sum_adds_rows(self):
        cache = GraphCache({"a": 1, "b": 1})
        cache.write_stream("a", _df(3), "pandas")
        cache.write_stream("b", _df(5), "pandas")
        assert cache.row_sum(["a", "b"]) == 8

    def test_row_sum_does_not_evict(self):
        cache = GraphCache({"a": 1})
        cache.write_stream("a", _df(3), "pandas")
        cache.row_sum(["a"])
        assert cache.get("a") is not None

    def test_row_sum_empty_keys(self):
        assert GraphCache({}).row_sum([]) == 0

    def test_row_sum_missing_key_counts_zero(self):
        cache = GraphCache({"a": 1})
        assert cache.row_sum(["nonexistent"]) == 0


class TestRowLimit:
    def test_write_stream_truncates(self):
        cache = GraphCache({"a": 1})
        cache.write_stream("a", _df(200), "pandas", row_limit=50)
        assert cache.get("a").num_rows == 50

    def test_write_stream_no_limit_passes_all(self):
        cache = GraphCache({"a": 1})
        cache.write_stream("a", _df(200), "pandas")
        assert cache.get("a").num_rows == 200

    def test_write_stream_limit_larger_than_data(self):
        cache = GraphCache({"a": 1})
        cache.write_stream("a", _df(10), "pandas", row_limit=100)
        assert cache.get("a").num_rows == 10


class TestSplitOps:
    def test_write_split_stores_port_keys(self):
        cache = GraphCache({"n.pass": 1, "n.fail": 1})
        result = {"pass": _df(2), "fail": _df(3)}
        total = cache.write_split("n", result, ("pass", "fail"), "pandas")
        assert total == 5
        assert cache.get("n.pass") is not None
        assert cache.get("n.fail") is not None

    def test_write_split_evicts_zero_consumer_ports(self):
        cache = GraphCache({"n.pass": 1, "n.fail": 0})
        result = {"pass": _df(2), "fail": _df(3)}
        cache.write_split("n", result, ("pass", "fail"), "pandas")
        assert cache.get("n.pass") is not None
        assert cache.get("n.fail") is None

    def test_write_split_row_limit(self):
        cache = GraphCache({"n.pass": 1})
        result = {"pass": _df(100)}
        cache.write_split("n", result, ("pass",), "pandas", row_limit=20)
        tbl = cache.get("n.pass")
        assert tbl is not None and tbl.num_rows == 20

    def test_write_split_missing_port_stores_none(self):
        cache = GraphCache({"n.pass": 1, "n.fail": 1})
        result = {"pass": _df(2)}
        cache.write_split("n", result, ("pass", "fail"), "pandas")
        assert cache.get("n.fail") is None

    def test_keep_preserves_split_port_with_zero_consumers(self):
        # Simulates preview: target is a split node, ports have 0 sub-consumers.
        cache = GraphCache({"n.pass": 0, "n.fail": 0}, keep={"n.pass", "n.fail"})
        result = {"pass": _df(3), "fail": _df(1)}
        cache.write_split("n", result, ("pass", "fail"), "pandas")
        assert cache.get("n.pass") is not None
        assert cache.get("n.fail") is not None


class TestSetRaw:
    def test_set_raw_none(self):
        cache = GraphCache({"a": 1})
        cache.set_raw("a", None)
        assert cache.get("a") is None

    def test_set_raw_table(self):
        cache = GraphCache({})
        cache.set_raw("x", _tbl(5))
        assert cache.get("x").num_rows == 5

    def test_set_raw_alias_port_to_node_key(self):
        # Simulates preview_graph split-pass alias.
        cache = GraphCache({"n.pass": 0}, keep={"n", "n.pass"})
        result = {"pass": _df(4)}
        cache.write_split("n", result, ("pass",), "pandas")
        cache.set_raw("n", cache.get("n.pass"))
        assert cache.get("n") is not None
        assert cache.get("n").num_rows == 4


class TestConsumeMissingKey:
    def test_returns_none_for_missing_key(self):
        cache = GraphCache({})
        assert cache.consume("nonexistent", "pandas") is None
