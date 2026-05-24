"""Integration tests for Sprint G graph ops: sql_run, sub_pipeline, iterate_fixed, iterate_loop.

All tests use only local files (no cloud credentials) so they run in CI
without any external service. iterate_files is covered via moto in
tests/connectors/test_s3.py.

Note on sub-pipeline YAML encoding:
    Template vars that land in integer-typed fields (e.g. limit.n) must be
    unquoted YAML literals so YAML parses them as int after substitution.
    Those sub-pipeline files are written as raw text rather than via
    yaml.safe_dump, which always quotes strings containing '{{'.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd
import pytest
import yaml

from decoy_engine import run_graph, validate_graph
from decoy_engine.exceptions import PipelineValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _yaml(d):
    return yaml.safe_dump(d)


@pytest.fixture
def work_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def input_csv(work_dir):
    """Five-row CSV with numeric + string columns."""
    path = os.path.join(work_dir, "input.csv")
    pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5],
            "name": ["Alice", "Bob", "Carol", "Dave", "Eve"],
            "score": [90, 75, 85, 60, 95],
            "dept": ["eng", "sales", "eng", "hr", "eng"],
        }
    ).to_csv(path, index=False)
    return path


@pytest.fixture
def out_path(work_dir):
    return os.path.join(work_dir, "output.csv")


# ---------------------------------------------------------------------------
# sql_run
# ---------------------------------------------------------------------------


class TestSqlRun:
    """DuckDB-on-DataFrame SQL escape hatch."""

    def test_select_all_is_passthrough(self, input_csv, out_path):
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {"id": "src", "kind": "source.file", "config": {"path": input_csv}},
                    {"id": "sql", "kind": "sql_run", "config": {"sql": "SELECT * FROM df"}},
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "src", "to": "sql"}, {"from": "sql", "to": "tgt"}],
            }
        )
        validate_graph(cfg)
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        assert len(out) == 5
        assert set(out.columns) == {"id", "name", "score", "dept"}

    def test_where_filters_rows(self, input_csv, out_path):
        """WHERE clause cuts rows; only high-scorers survive."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {"id": "src", "kind": "source.file", "config": {"path": input_csv}},
                    {
                        "id": "sql",
                        "kind": "sql_run",
                        "config": {
                            "sql": "SELECT * FROM df WHERE score > 80",
                        },
                    },
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "src", "to": "sql"}, {"from": "sql", "to": "tgt"}],
            }
        )
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        # Alice (90), Carol (85), Eve (95)
        assert len(out) == 3
        assert (out["score"] > 80).all()

    def test_projection_and_alias(self, input_csv, out_path):
        """Columns can be projected and renamed; source columns not selected are dropped."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {"id": "src", "kind": "source.file", "config": {"path": input_csv}},
                    {
                        "id": "sql",
                        "kind": "sql_run",
                        "config": {
                            "sql": "SELECT id, name, score * 2 AS double_score FROM df",
                        },
                    },
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "src", "to": "sql"}, {"from": "sql", "to": "tgt"}],
            }
        )
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        assert set(out.columns) == {"id", "name", "double_score"}
        alice_score = out.loc[out["name"] == "Alice", "double_score"].iloc[0]
        assert alice_score == 180

    def test_aggregate_group_by(self, input_csv, out_path):
        """GROUP BY aggregation shrinks the row count."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {"id": "src", "kind": "source.file", "config": {"path": input_csv}},
                    {
                        "id": "sql",
                        "kind": "sql_run",
                        "config": {
                            "sql": (
                                "SELECT dept, COUNT(*) AS headcount, AVG(score) AS avg_score "
                                "FROM df GROUP BY dept ORDER BY dept"
                            ),
                        },
                    },
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "src", "to": "sql"}, {"from": "sql", "to": "tgt"}],
            }
        )
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        assert set(out.columns) == {"dept", "headcount", "avg_score"}
        eng = out[out["dept"] == "eng"].iloc[0]
        assert eng["headcount"] == 3  # Alice, Carol, Eve

    def test_chained_after_mask(self, input_csv, out_path):
        """sql_run can sit downstream of a mask node and query the masked data."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {"id": "src", "kind": "source.file", "config": {"path": input_csv}},
                    {
                        "id": "msk",
                        "kind": "mask",
                        "config": {
                            "columns": {"name": {"strategy": "redact", "redact_with": "ANON"}},
                        },
                    },
                    {
                        "id": "sql",
                        "kind": "sql_run",
                        "config": {
                            "sql": "SELECT * FROM df WHERE name = 'ANON'",
                        },
                    },
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [
                    {"from": "src", "to": "msk"},
                    {"from": "msk", "to": "sql"},
                    {"from": "sql", "to": "tgt"},
                ],
            }
        )
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        # All rows were redacted to 'ANON', so all 5 should match.
        assert len(out) == 5
        assert (out["name"] == "ANON").all()

    def test_invalid_sql_surfaces_as_node_error(self, input_csv, out_path):
        """Malformed SQL produces a node-level error, not an unhandled exception."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {"id": "src", "kind": "source.file", "config": {"path": input_csv}},
                    {"id": "sql", "kind": "sql_run", "config": {"sql": "NOT VALID SQL @@@"}},
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "src", "to": "sql"}, {"from": "sql", "to": "tgt"}],
            }
        )
        result = run_graph(cfg)
        assert result["success"] is False
        failed = next(n for n in result["nodes"] if n["status"] == "error")
        assert failed["node_id"] == "sql"
        assert "sql_run" in failed["error"].lower()

    def test_reference_to_missing_table_surfaces_as_node_error(self, input_csv, out_path):
        """Referencing `foo` instead of `df` is caught at run time."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {"id": "src", "kind": "source.file", "config": {"path": input_csv}},
                    {"id": "sql", "kind": "sql_run", "config": {"sql": "SELECT * FROM foo"}},
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "src", "to": "sql"}, {"from": "sql", "to": "tgt"}],
            }
        )
        result = run_graph(cfg)
        assert result["success"] is False
        failed = next(n for n in result["nodes"] if n["status"] == "error")
        assert failed["node_id"] == "sql"

    @pytest.mark.parametrize("bad_sql", ["", "   ", None])
    def test_empty_or_null_sql_fails_validation(self, bad_sql):
        """Empty / null `sql` is caught at validate_graph time."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {"id": "src", "kind": "source.file", "config": {"path": "/tmp/x.csv"}},
                    {"id": "sql", "kind": "sql_run", "config": {"sql": bad_sql}},
                ],
                "edges": [{"from": "src", "to": "sql"}],
            }
        )
        with pytest.raises(PipelineValidationError):
            validate_graph(cfg)

    def test_missing_sql_key_fails_validation(self):
        """Missing `sql` key is caught at validate_graph time."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {"id": "src", "kind": "source.file", "config": {"path": "/tmp/x.csv"}},
                    {"id": "sql", "kind": "sql_run", "config": {}},
                ],
                "edges": [{"from": "src", "to": "sql"}],
            }
        )
        with pytest.raises(PipelineValidationError):
            validate_graph(cfg)


# ---------------------------------------------------------------------------
# sub_pipeline
# ---------------------------------------------------------------------------


class TestSubPipeline:
    """Call a sub-pipeline from within a parent graph."""

    def _write_sub(self, path, nodes, edges):
        Path(path).write_text(_yaml({"mode": "graph", "nodes": nodes, "edges": edges}))

    def test_output_flows_to_parent(self, work_dir, input_csv, out_path):
        """Sub-pipeline's output node becomes the parent node's output."""
        sub_path = os.path.join(work_dir, "sub.yaml")
        self._write_sub(
            sub_path,
            nodes=[
                {"id": "src", "kind": "source.file", "config": {"path": input_csv}},
                {"id": "filt", "kind": "filter", "config": {"predicate": "score > 80"}},
            ],
            edges=[{"from": "src", "to": "filt"}],
        )
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "sub",
                        "kind": "sub_pipeline",
                        "config": {"pipeline_ref": sub_path, "output_node": "filt"},
                    },
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "sub", "to": "tgt"}],
            }
        )
        validate_graph(cfg)
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        # Only Alice (90), Carol (85), Eve (95)
        assert len(out) == 3
        assert (out["score"] > 80).all()

    def test_template_vars_substituted_in_sub_yaml(self, work_dir, input_csv, out_path):
        """template_vars replace {{key}} placeholders in the sub-pipeline YAML text."""
        sub_path = os.path.join(work_dir, "sub_vars.yaml")
        # Write raw text so the {{dept}} placeholder is not escaped by yaml.safe_dump.
        Path(sub_path).write_text(
            f"mode: graph\n"
            f"nodes:\n"
            f"  - id: src\n"
            f"    kind: source.file\n"
            f"    config:\n"
            f"      path: '{input_csv}'\n"
            f"  - id: filt\n"
            f"    kind: filter\n"
            f"    config:\n"
            f"      predicate: \"dept == '{{{{dept}}}}'\""
            f"\nedges:\n"
            f"  - from: src\n"
            f"    to: filt\n"
        )
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "sub",
                        "kind": "sub_pipeline",
                        "config": {
                            "pipeline_ref": sub_path,
                            "output_node": "filt",
                            "template_vars": {"dept": "eng"},
                        },
                    },
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "sub", "to": "tgt"}],
            }
        )
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        assert (out["dept"] == "eng").all()
        assert len(out) == 3  # Alice, Carol, Eve

    def test_parent_can_transform_sub_output(self, work_dir, input_csv, out_path):
        """Parent graph can chain transforms after a sub_pipeline node."""
        sub_path = os.path.join(work_dir, "sub.yaml")
        self._write_sub(
            sub_path,
            nodes=[{"id": "src", "kind": "source.file", "config": {"path": input_csv}}],
            edges=[],
        )
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "sub",
                        "kind": "sub_pipeline",
                        "config": {"pipeline_ref": sub_path, "output_node": "src"},
                    },
                    # Drop a column in the parent graph, post sub-pipeline.
                    {"id": "drop", "kind": "drop_column", "config": {"columns": ["dept"]}},
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "sub", "to": "drop"}, {"from": "drop", "to": "tgt"}],
            }
        )
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        assert "dept" not in out.columns
        assert len(out) == 5

    def test_missing_pipeline_ref_errors_at_run_time(self, out_path):
        """Non-existent pipeline_ref raises at run time (not validate time)."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "sub",
                        "kind": "sub_pipeline",
                        "config": {"pipeline_ref": "/no/such/file.yaml", "output_node": "x"},
                    },
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "sub", "to": "tgt"}],
            }
        )
        # validate_graph cannot check that the file exists at validation time.
        validate_graph(cfg)
        result = run_graph(cfg)
        assert result["success"] is False
        failed = next(n for n in result["nodes"] if n["status"] == "error")
        assert failed["node_id"] == "sub"

    @pytest.mark.parametrize(
        "bad_config",
        [
            {"output_node": "out"},  # missing pipeline_ref
            {"pipeline_ref": "x.yaml"},  # missing output_node
            {},  # missing both
            {"pipeline_ref": "", "output_node": "out"},  # empty pipeline_ref
        ],
    )
    def test_invalid_config_fails_validation(self, bad_config):
        """Missing or empty pipeline_ref / output_node caught at validate time."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [{"id": "sub", "kind": "sub_pipeline", "config": bad_config}],
                "edges": [],
            }
        )
        with pytest.raises(PipelineValidationError):
            validate_graph(cfg)


# ---------------------------------------------------------------------------
# iterate_fixed
# ---------------------------------------------------------------------------


class TestIterateFixed:
    """Fan-out over a hardcoded list of values."""

    def _make_csv(self, path, rows):
        pd.DataFrame(rows).to_csv(path, index=False)
        return path

    def _write_path_sub(self, sub_path):
        """Sub-pipeline that reads {{iteration.value}} as a CSV path."""
        # Write raw text so {{iteration.value}} is not quoted by yaml.safe_dump.
        Path(sub_path).write_text(
            "mode: graph\n"
            "nodes:\n"
            "  - id: src\n"
            "    kind: source.file\n"
            "    config:\n"
            "      path: '{{iteration.value}}'\n"
            "edges: []\n"
        )

    def test_concat_over_file_paths(self, work_dir, out_path):
        """iterate_fixed with file paths as values reads each file and concats."""
        # Three small CSVs with one row each.
        paths = []
        for i, name in enumerate(["alice", "bob", "carol"], start=1):
            p = os.path.join(work_dir, f"{name}.csv")
            self._make_csv(p, {"id": [i], "name": [name]})
            paths.append(p)

        sub_path = os.path.join(work_dir, "sub.yaml")
        self._write_path_sub(sub_path)

        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "iter",
                        "kind": "iterate_fixed",
                        "config": {
                            "values": paths,
                            "pipeline_ref": sub_path,
                            "output_node": "src",
                            "output": "concat",
                        },
                    },
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "iter", "to": "tgt"}],
            }
        )
        validate_graph(cfg)
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        # 3 files x 1 row each
        assert len(out) == 3
        assert set(out["name"]) == {"alice", "bob", "carol"}

    def test_void_mode_runs_all_iterations(self, work_dir):
        """output='void' runs all iterations but emits no downstream rows."""
        # Sub-pipeline that writes to a per-iteration file.
        p = os.path.join(work_dir, "single.csv")
        pd.DataFrame({"x": [1]}).to_csv(p, index=False)

        sentinel_a = os.path.join(work_dir, "out_a.csv")
        sentinel_b = os.path.join(work_dir, "out_b.csv")

        # Sub-pipeline reads the single CSV and writes to {{iteration.value}}.
        sub_path = os.path.join(work_dir, "void_sub.yaml")
        Path(sub_path).write_text(
            f"mode: graph\n"
            f"nodes:\n"
            f"  - id: src\n"
            f"    kind: source.file\n"
            f"    config:\n"
            f"      path: '{p}'\n"
            f"  - id: tgt\n"
            f"    kind: target.file\n"
            f"    config:\n"
            f"      output_filename: '{{{{iteration.value}}}}'\n"
            f"edges:\n"
            f"  - from: src\n"
            f"    to: tgt\n"
        )
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "iter",
                        "kind": "iterate_fixed",
                        "config": {
                            "values": [sentinel_a, sentinel_b],
                            "pipeline_ref": sub_path,
                            "output_node": "tgt",
                            "output": "void",
                        },
                    },
                ],
                "edges": [],
            }
        )
        result = run_graph(cfg)
        assert result["success"], result
        # Both side-effect files were written by the sub-pipelines.
        assert os.path.exists(sentinel_a)
        assert os.path.exists(sentinel_b)

    def test_dict_values_expose_per_key_template_vars(self, work_dir, input_csv, out_path):
        """Dict values expose iteration.value.<key> placeholders."""
        # Sub-pipeline filters input by dept using {{iteration.value.dept}}.
        sub_path = os.path.join(work_dir, "dict_sub.yaml")
        Path(sub_path).write_text(
            f"mode: graph\n"
            f"nodes:\n"
            f"  - id: src\n"
            f"    kind: source.file\n"
            f"    config:\n"
            f"      path: '{input_csv}'\n"
            f"  - id: filt\n"
            f"    kind: filter\n"
            f"    config:\n"
            f"      predicate: \"dept == '{{{{iteration.value.dept}}}}'\""
            f"\nedges:\n"
            f"  - from: src\n"
            f"    to: filt\n"
        )
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "iter",
                        "kind": "iterate_fixed",
                        "config": {
                            "values": [{"dept": "eng"}, {"dept": "hr"}],
                            "pipeline_ref": sub_path,
                            "output_node": "filt",
                            "output": "concat",
                        },
                    },
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "iter", "to": "tgt"}],
            }
        )
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        # eng: Alice, Carol, Eve (3); hr: Dave (1) -> 4 total
        assert len(out) == 4
        assert set(out["dept"]) == {"eng", "hr"}

    def test_empty_values_fails_validation(self):
        """An empty `values` list is caught at validate_graph time."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "iter",
                        "kind": "iterate_fixed",
                        "config": {"values": [], "pipeline_ref": "x.yaml", "output_node": "out"},
                    }
                ],
                "edges": [],
            }
        )
        with pytest.raises(PipelineValidationError):
            validate_graph(cfg)

    def test_missing_values_fails_validation(self):
        """Missing `values` key is caught at validate_graph time."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "iter",
                        "kind": "iterate_fixed",
                        "config": {"pipeline_ref": "x.yaml", "output_node": "out"},
                    }
                ],
                "edges": [],
            }
        )
        with pytest.raises(PipelineValidationError):
            validate_graph(cfg)


# ---------------------------------------------------------------------------
# iterate_loop
# ---------------------------------------------------------------------------


class TestIterateLoop:
    """Fan-out over a numeric range."""

    def _write_limit_sub(self, sub_path, input_csv):
        """Sub-pipeline: source -> limit({{iteration.value}} rows).

        Written as raw text so that '{{iteration.value}}' in the YAML
        is an unquoted integer literal after substitution, which limit.py
        requires. yaml.safe_dump would wrap the placeholder in quotes,
        causing YAML to parse it as a string.
        """
        Path(sub_path).write_text(
            f"mode: graph\n"
            f"nodes:\n"
            f"  - id: src\n"
            f"    kind: source.file\n"
            f"    config:\n"
            f"      path: '{input_csv}'\n"
            f"  - id: lim\n"
            f"    kind: limit\n"
            f"    config:\n"
            f"      n: {{{{iteration.value}}}}\n"
            f"edges:\n"
            f"  - from: src\n"
            f"    to: lim\n"
        )

    def test_concat_sums_per_iteration_row_counts(self, work_dir, input_csv, out_path):
        """iterate_loop from 1..3 limits to 1, 2 rows -> 3 concat rows."""
        sub_path = os.path.join(work_dir, "loop_sub.yaml")
        self._write_limit_sub(sub_path, input_csv)
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "iter",
                        "kind": "iterate_loop",
                        "config": {
                            "start": 1,
                            "end": 3,  # range(1, 3) -> [1, 2]
                            "pipeline_ref": sub_path,
                            "output_node": "lim",
                            "output": "concat",
                        },
                    },
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "iter", "to": "tgt"}],
            }
        )
        validate_graph(cfg)
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        # limit(1) + limit(2) = 3 rows
        assert len(out) == 3

    def test_step_controls_iteration_count(self, work_dir, input_csv, out_path):
        """Non-default step skips values: range(2, 8, 3) -> [2, 5] -> 7 rows."""
        sub_path = os.path.join(work_dir, "step_sub.yaml")
        self._write_limit_sub(sub_path, input_csv)
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "iter",
                        "kind": "iterate_loop",
                        "config": {
                            "start": 2,
                            "end": 8,
                            "step": 3,  # values: 2, 5
                            "pipeline_ref": sub_path,
                            "output_node": "lim",
                            "output": "concat",
                        },
                    },
                    {"id": "tgt", "kind": "target.file", "config": {"output_filename": out_path}},
                ],
                "edges": [{"from": "iter", "to": "tgt"}],
            }
        )
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(out_path)
        # limit(2) + limit(5) = 7 rows
        assert len(out) == 7

    @pytest.mark.parametrize(
        "bad",
        [
            {"start": 5, "end": 1},  # start > end, positive step
            {"start": 5, "end": 5},  # equal, also invalid
        ],
    )
    def test_invalid_range_fails_validation(self, bad):
        """start >= end with a positive step is caught at validate_graph time."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "iter",
                        "kind": "iterate_loop",
                        "config": {**bad, "pipeline_ref": "x.yaml", "output_node": "out"},
                    }
                ],
                "edges": [],
            }
        )
        with pytest.raises(PipelineValidationError):
            validate_graph(cfg)

    def test_zero_step_fails_validation(self):
        """step=0 is caught at validate_graph time."""
        cfg = _yaml(
            {
                "mode": "graph",
                "nodes": [
                    {
                        "id": "iter",
                        "kind": "iterate_loop",
                        "config": {
                            "start": 0,
                            "end": 5,
                            "step": 0,
                            "pipeline_ref": "x.yaml",
                            "output_node": "out",
                        },
                    }
                ],
                "edges": [],
            }
        )
        with pytest.raises(PipelineValidationError):
            validate_graph(cfg)

    def test_missing_start_or_end_fails_validation(self):
        """Missing start or end is caught at validate_graph time."""
        for bad_config in [
            {"end": 5, "pipeline_ref": "x.yaml", "output_node": "out"},  # no start
            {"start": 0, "pipeline_ref": "x.yaml", "output_node": "out"},  # no end
        ]:
            cfg = _yaml(
                {
                    "mode": "graph",
                    "nodes": [{"id": "iter", "kind": "iterate_loop", "config": bad_config}],
                    "edges": [],
                }
            )
            with pytest.raises(PipelineValidationError):
                validate_graph(cfg)
