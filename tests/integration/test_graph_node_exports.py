"""End-to-end tests for the per-node exports feature.

Covers:
- Each of the v1 ops (source.file, mask, target.file, filter, generate,
  run_storm) emits its documented exports onto the NodeRunRecord.
- A downstream node's config can reference `${nodes.<id>.<key>}` and the
  runner resolves it against already-completed exports.
- Forward references / self-references raise actionable errors.
"""

import os
import tempfile

import pandas as pd
import pytest
import yaml

from decoy_engine import run_graph


@pytest.fixture
def tmpdir():
    return tempfile.mkdtemp()


@pytest.fixture
def csv_path(tmpdir):
    p = os.path.join(tmpdir, "in.csv")
    df = pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "state": ["CA", "NY", "CA", "TX", "CA"],
        "email": ["a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com"],
    })
    df.to_csv(p, index=False)
    return p


def _records_by_id(result):
    return {r["node_id"]: r for r in result["nodes"]}


def test_source_file_emits_row_count_and_format(csv_path, tmpdir):
    yaml_text = yaml.safe_dump({
        "mode": "graph",
        "engine": "pandas",  # deterministic across substrates
        "nodes": [
            {"id": "src1", "kind": "source.file", "config": {"path": csv_path}},
            {"id": "tgt1", "kind": "target.file", "config": {
                "output_filename": os.path.join(tmpdir, "out.csv"),
            }},
        ],
        "edges": [{"from": "src1", "to": "tgt1"}],
    })
    result = run_graph(yaml_text)
    assert result["success"], result
    rec = _records_by_id(result)
    assert rec["src1"]["exports"]["row_count"] == 5
    assert rec["src1"]["exports"]["column_count"] == 3
    assert rec["src1"]["exports"]["inferred_format"] == "csv"
    assert rec["src1"]["exports"]["file_size_bytes"] > 0
    assert rec["tgt1"]["exports"]["rows_written"] == 5
    assert rec["tgt1"]["exports"]["output_path"].endswith("out.csv")
    assert rec["tgt1"]["exports"]["output_file_size_bytes"] > 0


def test_filter_emits_selectivity(csv_path, tmpdir):
    yaml_text = yaml.safe_dump({
        "mode": "graph",
        "engine": "pandas",
        "nodes": [
            {"id": "src1", "kind": "source.file", "config": {"path": csv_path}},
            {"id": "flt1", "kind": "filter", "config": {"predicate": "state == 'CA'"}},
            {"id": "tgt1", "kind": "target.file", "config": {
                "output_filename": os.path.join(tmpdir, "out.csv"),
            }},
        ],
        "edges": [
            {"from": "src1", "to": "flt1"},
            {"from": "flt1", "to": "tgt1"},
        ],
    })
    result = run_graph(yaml_text)
    assert result["success"], result
    rec = _records_by_id(result)
    assert rec["flt1"]["exports"]["rows_in"] == 5
    assert rec["flt1"]["exports"]["rows_out"] == 3
    assert rec["flt1"]["exports"]["selectivity"] == pytest.approx(0.6)


def test_mask_emits_rows_processed_and_strategies(csv_path, tmpdir):
    yaml_text = yaml.safe_dump({
        "mode": "graph",
        "engine": "pandas",
        "nodes": [
            {"id": "src1", "kind": "source.file", "config": {"path": csv_path}},
            {"id": "mask1", "kind": "mask", "config": {
                "columns": {
                    "email": {"strategy": "hash"},
                    "state": {"strategy": "faker", "faker_type": "state_abbr"},
                },
            }},
            {"id": "tgt1", "kind": "target.file", "config": {
                "output_filename": os.path.join(tmpdir, "out.csv"),
            }},
        ],
        "edges": [
            {"from": "src1", "to": "mask1"},
            {"from": "mask1", "to": "tgt1"},
        ],
    })
    result = run_graph(yaml_text)
    assert result["success"], result
    rec = _records_by_id(result)
    exports = rec["mask1"]["exports"]
    assert exports["rows_processed"] == 5
    assert sorted(exports["strategies_applied"]) == ["faker", "hash"]
    assert exports["null_passthrough_count"] == 0


def test_generate_emits_seed_and_counts(tmpdir):
    yaml_text = yaml.safe_dump({
        "mode": "graph",
        "engine": "pandas",
        "nodes": [
            {"id": "gen1", "kind": "generate", "config": {
                "row_count": 25,
                "seed": 7,
                "columns": {
                    "name": {"strategy": "faker", "faker_type": "first_name"},
                    "idx": {"strategy": "sequence", "start": 1, "step": 1},
                },
            }},
            {"id": "tgt1", "kind": "target.file", "config": {
                "output_filename": os.path.join(tmpdir, "out.csv"),
            }},
        ],
        "edges": [{"from": "gen1", "to": "tgt1"}],
    })
    result = run_graph(yaml_text)
    assert result["success"], result
    rec = _records_by_id(result)
    assert rec["gen1"]["exports"]["rows_generated"] == 25
    assert rec["gen1"]["exports"]["columns_generated"] == 2
    assert rec["gen1"]["exports"]["seed_used"] == 7


def test_node_export_resolves_in_downstream_config(csv_path, tmpdir):
    """source.file emits row_count=5; target.file's output_filename
    interpolates it into the path. Validates the live `${nodes.X.Y}`
    resolver."""
    out_pattern = os.path.join(tmpdir, "out_${nodes.src1.row_count}.csv")
    yaml_text = yaml.safe_dump({
        "mode": "graph",
        "engine": "pandas",
        "nodes": [
            {"id": "src1", "kind": "source.file", "config": {"path": csv_path}},
            {"id": "tgt1", "kind": "target.file", "config": {
                "output_filename": out_pattern,
            }},
        ],
        "edges": [{"from": "src1", "to": "tgt1"}],
    })
    result = run_graph(yaml_text)
    assert result["success"], result
    expected = os.path.join(tmpdir, "out_5.csv")
    assert os.path.exists(expected), f"expected {expected}, files: {os.listdir(tmpdir)}"


def test_whole_string_token_preserves_int_type(csv_path):
    """A config value that is exactly `${nodes.X.Y}` (no surrounding
    text) preserves the export's native type — int stays int, not str."""
    from decoy_engine.graph.runner import _resolve_node_exports

    exports = {"src1": {"row_count": 42}}
    resolved = _resolve_node_exports(
        {"limit": "${nodes.src1.row_count}", "prefix": "n=${nodes.src1.row_count}"},
        exports,
        current_node_id="tgt1",
    )
    assert resolved["limit"] == 42
    assert isinstance(resolved["limit"], int)
    assert resolved["prefix"] == "n=42"


def test_forward_reference_raises_clear_error(csv_path, tmpdir):
    """target.file references a downstream-yet-to-run node — runner
    fails fast with a clear error rather than running with a placeholder."""
    yaml_text = yaml.safe_dump({
        "mode": "graph",
        "engine": "pandas",
        "nodes": [
            {"id": "src1", "kind": "source.file", "config": {"path": csv_path}},
            {"id": "tgt1", "kind": "target.file", "config": {
                "output_filename": os.path.join(
                    tmpdir, "out_${nodes.future.row_count}.csv"
                ),
            }},
        ],
        "edges": [{"from": "src1", "to": "tgt1"}],
    })
    result = run_graph(yaml_text)
    assert not result["success"]
    rec = _records_by_id(result)
    assert "future" in rec["tgt1"]["error"]
    assert "has not run yet" in rec["tgt1"]["error"]


def test_self_reference_raises_clear_error(csv_path, tmpdir):
    """A node referencing its own exports is a configuration error —
    exports are only readable from strictly-downstream nodes."""
    yaml_text = yaml.safe_dump({
        "mode": "graph",
        "engine": "pandas",
        "nodes": [
            {"id": "src1", "kind": "source.file", "config": {"path": csv_path}},
            {"id": "tgt1", "kind": "target.file", "config": {
                "output_filename": os.path.join(
                    tmpdir, "out_${nodes.tgt1.row_count}.csv"
                ),
            }},
        ],
        "edges": [{"from": "src1", "to": "tgt1"}],
    })
    result = run_graph(yaml_text)
    assert not result["success"]
    rec = _records_by_id(result)
    assert "tgt1" in rec["tgt1"]["error"]
    assert "own exports" in rec["tgt1"]["error"]
