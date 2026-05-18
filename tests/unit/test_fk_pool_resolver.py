"""Sprint 4 Commit 2: FK pool resolver unit tests.

Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG; materialize
parent pool; child samples with replacement.

Covers the closure built by _build_pool_resolver in graph/runner.py.
The closure is what generate_op.apply will call (Commit 4) to draw FK
values from a parent node's distinct PK pool.

Tests this module owns:
  - Pool resolver returns distinct non-null values from a parent cache
    entry.
  - Parent stays alive in cache when included in the keep_set, even
    after the normal consumer count drains to zero (pinning behavior
    is the cache's; resolver verifies the integration).
  - Empty parent (all-null column or empty table) raises
    EmptyParentPoolError.
  - Unknown column on parent raises UnknownFKColumnError.
"""
from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.exceptions import (
    EmptyParentPoolError,
    UnknownFKColumnError,
)
from decoy_engine.graph.cache import GraphCache
from decoy_engine.graph.runner import _build_pool_resolver


def _cache_with(table: pa.Table, node_id: str, *, keep: set[str] | None = None) -> GraphCache:
    """Helper: build a cache with a single parent node already written.
    Consumer count starts at 0 so the parent only stays alive via the
    keep_set (which is what the runner extends with FK parents)."""
    keep = keep or set()
    cache = GraphCache({node_id: 0}, keep=keep)
    cache.write_stream(node_id, table.to_pandas(), "pandas")
    return cache


class TestPoolResolverReadsParent:
    def test_returns_distinct_non_null_values(self):
        # Parent column has duplicates + nulls; resolver returns the
        # distinct non-null set.
        tbl = pa.table({"customer_id": [1, 2, 2, 3, None, 1, 4]})
        cache = _cache_with(tbl, "mask_1", keep={"mask_1"})
        resolver = _build_pool_resolver(cache, by_id={"mask_1": {}})

        pool = resolver("mask_1", "customer_id")
        assert sorted(pool) == [1, 2, 3, 4]

    def test_preserves_order_for_single_pass(self):
        tbl = pa.table({"x": [10, 20, 30, 20, 40]})
        cache = _cache_with(tbl, "p", keep={"p"})
        resolver = _build_pool_resolver(cache, by_id={"p": {}})

        pool = resolver("p", "x")
        # pyarrow.compute.unique preserves first-occurrence order.
        assert pool == [10, 20, 30, 40]


class TestPoolResolverPinnedAgainstEviction:
    def test_parent_in_keep_set_survives_zero_consumers(self):
        """Cache pinning is what the runner does in _execute_graph
        when it extends keep_set with parent node ids from
        column_relationships. The resolver itself relies on this:
        without pinning, the parent table would have been evicted
        when its normal consumer count drained to zero."""
        tbl = pa.table({"id": [1, 2, 3]})
        # consumer_counts={parent: 1} means there's one normal
        # downstream consumer. After it consumes, the cache would
        # normally evict -- unless keep_set has the parent.
        cache = GraphCache({"parent": 1}, keep={"parent"})
        cache.write_stream("parent", tbl.to_pandas(), "pandas")

        # Simulate the normal consumer reading the parent.
        cache.consume("parent", "pandas")
        # Cache hasn't been told about "parent" being released -- but
        # because we put it in the keep_set, it should still be there
        # for the FK resolver.
        assert cache.get("parent") is not None, (
            "parent should stay alive in cache because it's in keep_set, "
            "even after normal consumers drained"
        )

        resolver = _build_pool_resolver(cache, by_id={"parent": {}})
        pool = resolver("parent", "id")
        assert sorted(pool) == [1, 2, 3]


class TestPoolResolverEmptyPoolRaises:
    def test_all_null_column_raises_empty_parent_pool(self):
        tbl = pa.table({"customer_id": [None, None, None]})
        cache = _cache_with(tbl, "mask_1", keep={"mask_1"})
        resolver = _build_pool_resolver(cache, by_id={"mask_1": {}})

        with pytest.raises(EmptyParentPoolError) as exc_info:
            resolver("mask_1", "customer_id")
        assert exc_info.value.parent_node == "mask_1"
        assert exc_info.value.parent_column == "customer_id"

    def test_empty_table_raises_empty_parent_pool(self):
        tbl = pa.table({"customer_id": pa.array([], type=pa.int64())})
        cache = _cache_with(tbl, "mask_1", keep={"mask_1"})
        resolver = _build_pool_resolver(cache, by_id={"mask_1": {}})

        with pytest.raises(EmptyParentPoolError):
            resolver("mask_1", "customer_id")


class TestPoolResolverUnknownColumnRaises:
    def test_missing_column_raises_unknown_fk_column(self):
        tbl = pa.table({"id": [1, 2, 3]})  # no customer_id column
        cache = _cache_with(tbl, "mask_1", keep={"mask_1"})
        resolver = _build_pool_resolver(cache, by_id={"mask_1": {}})

        with pytest.raises(UnknownFKColumnError) as exc_info:
            resolver("mask_1", "customer_id")
        # Error carries the column name + parent node so the manifest
        # entry knows which FK failed.
        assert exc_info.value.parent_node == "mask_1"
        assert exc_info.value.parent_column == "customer_id"
        # Available columns surfaced in the message so an operator
        # can spot the typo / drift quickly.
        assert "id" in str(exc_info.value)

    def test_unknown_parent_node_raises_unknown_fk_column(self):
        """Unknown parent (nothing in cache) maps to the same code as
        an unknown column on a known parent -- the runtime can't tell
        the operator anything different at this layer; the validation
        stage in Commit 3 catches the more specific 'unknown_node'
        case earlier."""
        cache = GraphCache({"other": 0}, keep={"other"})
        cache.write_stream("other", pd.DataFrame({"x": [1]}), "pandas")
        resolver = _build_pool_resolver(cache, by_id={"other": {}})

        with pytest.raises(UnknownFKColumnError) as exc_info:
            resolver("ghost_node", "anything")
        assert exc_info.value.parent_node == "ghost_node"
