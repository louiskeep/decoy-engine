"""Phase 5 of polars-duckdb hybrid plan: preview output is identical shape
regardless of which engine produced the table.

The whole point of the preview-boundary serialization in runner.py is "no
UX regression." The UI sees `{ columns, rows, ... }` and shouldn't care
whether DuckDB or pandas read the source.
"""

from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest
import yaml

from decoy_engine import preview_graph


@pytest.fixture
def tmp_csv():
    tmpdir = tempfile.mkdtemp()
    src = os.path.join(tmpdir, "in.csv")
    pd.DataFrame({
        "id": [1, 2, 3, 4],
        "state": ["CA", "NY", "CA", "TX"],
        "value": [10, 20, 30, 40],
    }).to_csv(src, index=False)
    return src


def _preview_with_engine(tmp_csv, engine: str | None, target: str = "f"):
    cfg = {
        "mode": "graph",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": tmp_csv}},
            {"id": "f", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
        ],
        "edges": [{"from": "s", "to": "f"}],
    }
    if engine is not None:
        cfg["engine"] = engine
    return preview_graph(yaml.safe_dump(cfg), target, row_limit=10)


def test_preview_columns_identical_across_engines(tmp_csv):
    p_pandas = _preview_with_engine(tmp_csv, "pandas")
    p_hybrid = _preview_with_engine(tmp_csv, "hybrid")
    assert p_pandas["columns"] == p_hybrid["columns"]


def test_preview_row_count_identical_across_engines(tmp_csv):
    p_pandas = _preview_with_engine(tmp_csv, "pandas")
    p_hybrid = _preview_with_engine(tmp_csv, "hybrid")
    assert p_pandas["row_count"] == p_hybrid["row_count"]


def test_preview_rows_identical_across_engines(tmp_csv):
    """The actual data shape — list-of-lists, JSON-friendly — must match."""
    p_pandas = _preview_with_engine(tmp_csv, "pandas")
    p_hybrid = _preview_with_engine(tmp_csv, "hybrid")
    # Sort rows by id so any internal ordering differences don't mask
    # actual content mismatches.
    p_rows_sorted = sorted(p_pandas["rows"], key=lambda r: r[0])
    h_rows_sorted = sorted(p_hybrid["rows"], key=lambda r: r[0])
    assert p_rows_sorted == h_rows_sorted


def test_preview_applied_chain_identical_across_engines(tmp_csv):
    p_pandas = _preview_with_engine(tmp_csv, "pandas")
    p_hybrid = _preview_with_engine(tmp_csv, "hybrid")
    assert p_pandas["applied_chain"] == p_hybrid["applied_chain"]


def test_preview_at_source_node_identical_across_engines(tmp_csv):
    """Preview at the source itself — no transform — should also match."""
    p_pandas = _preview_with_engine(tmp_csv, "pandas", target="s")
    p_hybrid = _preview_with_engine(tmp_csv, "hybrid", target="s")
    assert p_pandas["columns"] == p_hybrid["columns"]
    assert p_pandas["row_count"] == p_hybrid["row_count"]


def test_preview_error_carries_friendly_message_in_hybrid(tmp_csv):
    """A deliberately-broken pipeline produces a translated error msg
    instead of a raw polars / duckdb traceback."""
    cfg = yaml.safe_dump({
        "mode": "graph",
        "engine": "hybrid",
        "nodes": [
            {"id": "s", "kind": "source.file", "config": {"path": tmp_csv}},
            {"id": "so", "kind": "sort", "config": {"by": ["does_not_exist"]}},
        ],
        "edges": [{"from": "s", "to": "so"}],
    })
    p = preview_graph(cfg, "so", row_limit=10)
    assert p["error"] is not None
    # Error message should reference the column the user was asking for —
    # that's what makes it actionable.
    assert "does_not_exist" in p["error"]
    # Should also include node context so the canvas can highlight the
    # right node.
    assert "'so'" in p["error"]
