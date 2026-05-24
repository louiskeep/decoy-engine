"""Phase 1 of polars-duckdb hybrid plan: Arrow-canonical runner cache.

These tests are about the runner's internal state, not just end-to-end
correctness. We exercise:
  - cache holds pyarrow.Table between ops
  - per-node consumer count drops to zero exactly once per upstream
  - cache entry is evicted at zero-consumer
  - branching graphs keep an upstream entry alive until the last consumer
  - preview mode pins the target node's entry against eviction
"""

from __future__ import annotations

import os
import tempfile

import pandas as pd
import pyarrow as pa
import pytest
import yaml

from decoy_engine import preview_graph, run_graph, validate_graph
from decoy_engine.graph.cache import GraphCache
from decoy_engine.graph.conversion import (
    arrow_to_engine,
    engine_to_arrow,
)
from decoy_engine.graph.ops import OPS
from decoy_engine.graph.planner import _count_consumers


@pytest.fixture
def tmp_csv():
    """Six-row CSV; same shape as test_graph.py for consistency."""
    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, "in.csv")
    out = os.path.join(tmpdir, "out.csv")
    df = pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5, 5],
            "state": ["CA", "NY", "CA", "TX", "CA", "CA"],
            "value": [10, 20, 30, 40, 50, 50],
        }
    )
    df.to_csv(src, index=False)
    return src, out


def _yaml(d: dict) -> str:
    return yaml.safe_dump(d)


# -------- conversion shim ---------------------------------------------------


def test_arrow_pandas_roundtrip_preserves_data():
    """The pandas branch intentionally round-trips through Arrow-backed dtypes."""
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    table = engine_to_arrow(df, "pandas")
    assert isinstance(table, pa.Table)
    back = arrow_to_engine(table, "pandas")
    pd.testing.assert_frame_equal(back, df, check_dtype=False)


def test_arrow_pass_through_for_arrow_engine():
    df = pd.DataFrame({"a": [1, 2, 3]})
    table = engine_to_arrow(df, "pandas")
    out = arrow_to_engine(table, "arrow")
    assert out is table


def test_engine_to_arrow_rejects_wrong_type_for_pandas():
    with pytest.raises(TypeError):
        engine_to_arrow("not-a-dataframe", "pandas")


def test_arrow_to_engine_rejects_unknown_engine():
    df = pd.DataFrame({"a": [1]})
    table = engine_to_arrow(df, "pandas")
    with pytest.raises(ValueError):
        arrow_to_engine(table, "klingon")  # type: ignore[arg-type]


# -------- consumer count ----------------------------------------------------


def test_count_consumers_linear():
    nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    edges = [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]
    counts = _count_consumers(nodes, edges, OPS)
    assert counts == {"a": 1, "b": 1, "c": 0}


def test_count_consumers_branching():
    """A feeds B and C; both feed D. A has 2 consumers."""
    nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
    edges = [
        {"from": "a", "to": "b"},
        {"from": "a", "to": "c"},
        {"from": "b", "to": "d"},
        {"from": "c", "to": "d"},
    ]
    counts = _count_consumers(nodes, edges, OPS)
    assert counts == {"a": 2, "b": 1, "c": 1, "d": 0}


def test_count_consumers_self_loop_ignored():
    """Self-loops are forbidden, but the counter should stay sane."""
    nodes = [{"id": "a"}, {"id": "b"}]
    edges = [
        {"from": "a", "to": "a"},
        {"from": "a", "to": "b"},
    ]
    counts = _count_consumers(nodes, edges, OPS)
    assert counts == {"a": 1, "b": 0}


# -------- GraphCache eviction ----------------------------------------------


def test_consume_evicts_at_zero_consumers():
    table = pa.table({"x": [1, 2]})
    cache = GraphCache({"a": 1})
    cache.set_raw("a", table)

    out = cache.consume("a", "pandas")
    assert isinstance(out, pd.DataFrame)
    assert "a" not in cache._data
    assert cache._remaining["a"] == 0


def test_consume_keeps_entry_with_remaining_consumers():
    table = pa.table({"x": [1, 2]})
    cache = GraphCache({"a": 2})
    cache.set_raw("a", table)

    _ = cache.consume("a", "pandas")
    assert "a" in cache._data
    assert cache._remaining["a"] == 1

    _ = cache.consume("a", "pandas")
    assert "a" not in cache._data
    assert cache._remaining["a"] == 0


def test_consume_respects_hold_for_preview_target():
    """A held preview target must survive after its consumer count hits zero."""
    table = pa.table({"x": [1, 2]})
    cache = GraphCache({"target": 1})
    cache.set_raw("target", table)

    _ = cache.consume("target", "pandas", hold="target")
    assert "target" in cache._data


# -------- end-to-end runner behavior ---------------------------------------


def test_run_graph_arrow_cache_does_not_break_existing_pipelines(tmp_csv):
    """The whole point of Phase 1: existing ops keep working unchanged."""
    src, out = tmp_csv
    cfg = _yaml(
        {
            "mode": "graph",
            "nodes": [
                {"id": "s", "kind": "source.file", "config": {"path": src}},
                {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
                {"id": "d", "kind": "dedupe", "config": {"on": ["id"]}},
                {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
            ],
            "edges": [
                {"from": "s", "to": "f"},
                {"from": "f", "to": "d"},
                {"from": "d", "to": "t"},
            ],
        }
    )
    validate_graph(cfg)
    result = run_graph(cfg)
    assert result["success"] is True
    written = pd.read_csv(out)
    assert (written["state"] == "CA").all()
    assert sorted(written["id"].tolist()) == [1, 3, 5]


def test_preview_branching_graph_returns_correct_target(tmp_csv):
    """Branching graph: source feeds two filters; preview either."""
    src, _ = tmp_csv
    cfg = _yaml(
        {
            "mode": "graph",
            "nodes": [
                {"id": "s", "kind": "source.file", "config": {"path": src}},
                {"id": "ca", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
                {"id": "ny", "kind": "filter", "config": {"predicate": "state == 'NY'"}},
            ],
            "edges": [
                {"from": "s", "to": "ca"},
                {"from": "s", "to": "ny"},
            ],
        }
    )
    p_ca = preview_graph(cfg, "ca", row_limit=10)
    p_ny = preview_graph(cfg, "ny", row_limit=10)
    assert p_ca["row_count"] == 4
    assert p_ny["row_count"] == 1


def test_run_graph_records_arrow_row_count(tmp_csv):
    src, out = tmp_csv
    cfg = _yaml(
        {
            "mode": "graph",
            "nodes": [
                {"id": "s", "kind": "source.file", "config": {"path": src}},
                {"id": "t", "kind": "target.file", "config": {"output_filename": out}},
            ],
            "edges": [{"from": "s", "to": "t"}],
        }
    )
    result = run_graph(cfg)
    assert result["success"] is True
    s_record = next(n for n in result["nodes"] if n["node_id"] == "s")
    assert s_record["row_count"] == 6
