"""Tests for sub_pipeline and the three iterator ops.

The four ops share a lot of machinery (the runner refactor + the
shared `_iterator_core` helper), so tests live in one module to keep
the cross-op fixtures (temp YAML files, etc.) DRY. Per-op behavior is
asserted in dedicated classes.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.errors import ValidationError
from decoy_engine.graph.ops import (
    OPS,
    iterate_files,
    iterate_fixed,
    iterate_loop,
    sub_pipeline,
)
from decoy_engine.graph.ops._base import OpError

# ----- Helpers -----------------------------------------------------------


def _write_yaml(tmp_path: Path, name: str, body: str) -> Path:
    """Write a YAML file under tmp_path and return its path. Dedents body."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _make_csv(tmp_path: Path, name: str, rows: str) -> Path:
    """Write a tiny CSV under tmp_path; rows is the full CSV body."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(rows), encoding="utf-8")
    return p


# ----- sub_pipeline ------------------------------------------------------


class TestSubPipelineValidation:
    def test_missing_pipeline_ref_rejected(self):
        with pytest.raises(ValidationError):
            sub_pipeline.validate_config({"output_node": "out"})

    def test_missing_output_node_rejected(self):
        with pytest.raises(ValidationError):
            sub_pipeline.validate_config({"pipeline_ref": "x.yaml"})

    def test_non_dict_template_vars_rejected(self):
        with pytest.raises(ValidationError):
            sub_pipeline.validate_config(
                {
                    "pipeline_ref": "x.yaml",
                    "output_node": "out",
                    "template_vars": "not-a-dict",
                }
            )

    def test_valid_config_passes(self):
        sub_pipeline.validate_config({"pipeline_ref": "x.yaml", "output_node": "out"})


class TestSubPipelineDepthCap:
    """The runner caps sub_pipeline recursion so A -> B -> A cycles can't
    hang a worker."""

    def test_depth_cap_default_is_32(self):
        from decoy_engine.graph.ops.sub_pipeline import MAX_SUB_PIPELINE_DEPTH

        assert MAX_SUB_PIPELINE_DEPTH == 32

    def test_self_referential_sub_pipeline_raises_at_cap(self, tmp_path):
        # A pipeline whose only node is a sub_pipeline pointing at itself.
        # Will recurse forever without the depth guard.
        cycle_yaml = tmp_path / "cycle.yaml"
        cycle_yaml.write_text(
            "mode: graph\n"
            "nodes:\n"
            "  - id: spin\n"
            "    kind: sub_pipeline\n"
            "    config:\n"
            f"      pipeline_ref: {cycle_yaml}\n"
            "      output_node: spin\n"
            "edges: []\n",
            encoding="utf-8",
        )
        with pytest.raises(OpError, match="depth limit exceeded"):
            sub_pipeline.apply(
                inputs=[],
                config={
                    "pipeline_ref": str(cycle_yaml),
                    "output_node": "spin",
                },
                ctx=None,
            )


class TestSubPipelineApply:
    def test_runs_sub_graph_and_returns_output_node(self, tmp_path):
        csv = _make_csv(
            tmp_path,
            "src.csv",
            """\
            id,name
            1,alice
            2,bob
            """,
        )
        sub_yaml = _write_yaml(
            tmp_path,
            "sub.yaml",
            f"""\
            mode: graph
            nodes:
              - id: src
                kind: source.file
                config:
                  path: {csv}
            edges: []
            """,
        )

        table = sub_pipeline.apply(
            inputs=[],
            config={"pipeline_ref": str(sub_yaml), "output_node": "src"},
            ctx=None,
        )
        assert isinstance(table, pa.Table)
        assert table.num_rows == 2
        assert set(table.column_names) == {"id", "name"}

    def test_ref_not_found_raises_op_error(self):
        with pytest.raises(OpError, match="not found"):
            sub_pipeline.apply(
                inputs=[],
                config={"pipeline_ref": "no-such-file.yaml", "output_node": "x"},
                ctx=None,
            )

    def test_missing_output_node_in_subgraph_raises(self, tmp_path):
        csv = _make_csv(tmp_path, "src.csv", "id\n1\n")
        sub_yaml = _write_yaml(
            tmp_path,
            "sub.yaml",
            f"""\
            mode: graph
            nodes:
              - id: src
                kind: source.file
                config:
                  path: {csv}
            edges: []
            """,
        )
        with pytest.raises(OpError, match="no output"):
            sub_pipeline.apply(
                inputs=[],
                config={
                    "pipeline_ref": str(sub_yaml),
                    "output_node": "does-not-exist",
                },
                ctx=None,
            )

    def test_template_vars_substituted_in_yaml(self, tmp_path):
        # Build two CSVs; the sub-pipeline picks one via template var.
        _make_csv(tmp_path, "src-A.csv", "id\n1\n2\n")
        _make_csv(tmp_path, "src-B.csv", "id\n10\n20\n30\n")
        sub_yaml_text = (
            "mode: graph\n"
            "nodes:\n"
            f"  - id: src\n"
            f"    kind: source.file\n"
            f"    config:\n"
            f"      path: {tmp_path}/src-{{{{which}}}}.csv\n"
            "edges: []\n"
        )
        sub_path = tmp_path / "sub.yaml"
        sub_path.write_text(sub_yaml_text, encoding="utf-8")

        table = sub_pipeline.apply(
            inputs=[],
            config={
                "pipeline_ref": str(sub_path),
                "output_node": "src",
                "template_vars": {"which": "B"},
            },
            ctx=None,
        )
        assert table.num_rows == 3


# ----- iterate_fixed -----------------------------------------------------


class TestIterateFixedValidation:
    def test_empty_values_rejected(self):
        with pytest.raises(ValidationError):
            iterate_fixed.validate_config(
                {
                    "values": [],
                    "pipeline_ref": "x.yaml",
                    "output_node": "out",
                }
            )

    def test_non_list_values_rejected(self):
        with pytest.raises(ValidationError):
            iterate_fixed.validate_config(
                {
                    "values": "not-a-list",
                    "pipeline_ref": "x.yaml",
                    "output_node": "out",
                }
            )

    def test_bad_output_mode_rejected(self):
        with pytest.raises(ValidationError):
            iterate_fixed.validate_config(
                {
                    "values": [1, 2, 3],
                    "pipeline_ref": "x.yaml",
                    "output_node": "out",
                    "output": "bogus",
                }
            )


class TestIterateFixedApply:
    def test_iterates_and_concats(self, tmp_path):
        # Sub-pipeline reads a CSV whose name depends on the iteration value.
        for which in ("A", "B", "C"):
            _make_csv(tmp_path, f"src-{which}.csv", f"id\n{which}\n")
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "mode: graph\n"
            "nodes:\n"
            "  - id: src\n"
            "    kind: source.file\n"
            "    config:\n"
            f"      path: {tmp_path}/src-{{{{iteration.value}}}}.csv\n"
            "edges: []\n",
            encoding="utf-8",
        )

        table = iterate_fixed.apply(
            inputs=[],
            config={
                "values": ["A", "B", "C"],
                "pipeline_ref": str(sub_yaml),
                "output_node": "src",
            },
            ctx=None,
        )
        # Three iterations vstacked: 1 row each, 3 total.
        assert table.num_rows == 3
        ids = table.column("id").to_pylist()
        assert ids == ["A", "B", "C"]  # preserved order

    def test_void_output_returns_empty(self, tmp_path):
        # Same setup but output: void; should return zero-row table.
        for which in ("X", "Y"):
            _make_csv(tmp_path, f"src-{which}.csv", f"id\n{which}\n")
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "mode: graph\n"
            "nodes:\n"
            "  - id: src\n"
            "    kind: source.file\n"
            "    config:\n"
            f"      path: {tmp_path}/src-{{{{iteration.value}}}}.csv\n"
            "edges: []\n",
            encoding="utf-8",
        )

        table = iterate_fixed.apply(
            inputs=[],
            config={
                "values": ["X", "Y"],
                "pipeline_ref": str(sub_yaml),
                "output_node": "src",
                "output": "void",
            },
            ctx=None,
        )
        assert table.num_rows == 0

    def test_dict_values_expose_keyed_template_vars(self, tmp_path):
        # values list contains dicts; sub-pipeline references one dict key.
        for prefix in ("alpha", "beta"):
            _make_csv(tmp_path, f"src-{prefix}.csv", "id\n1\n")
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "mode: graph\n"
            "nodes:\n"
            "  - id: src\n"
            "    kind: source.file\n"
            "    config:\n"
            f"      path: {tmp_path}/src-{{{{iteration.value.name}}}}.csv\n"
            "edges: []\n",
            encoding="utf-8",
        )

        table = iterate_fixed.apply(
            inputs=[],
            config={
                "values": [{"name": "alpha"}, {"name": "beta"}],
                "pipeline_ref": str(sub_yaml),
                "output_node": "src",
            },
            ctx=None,
        )
        assert table.num_rows == 2

    def test_fail_fast_on_iteration_error(self, tmp_path):
        # First iteration's CSV exists; second points at a missing file.
        _make_csv(tmp_path, "src-good.csv", "id\n1\n")
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "mode: graph\n"
            "nodes:\n"
            "  - id: src\n"
            "    kind: source.file\n"
            "    config:\n"
            f"      path: {tmp_path}/src-{{{{iteration.value}}}}.csv\n"
            "edges: []\n",
            encoding="utf-8",
        )

        with pytest.raises(OpError, match="iteration 1"):
            iterate_fixed.apply(
                inputs=[],
                config={
                    "values": ["good", "missing"],
                    "pipeline_ref": str(sub_yaml),
                    "output_node": "src",
                },
                ctx=None,
            )


# ----- iterate_loop ------------------------------------------------------


class TestIterateLoopValidation:
    def test_missing_start_rejected(self):
        with pytest.raises(ValidationError):
            iterate_loop.validate_config({"end": 5, "pipeline_ref": "x.yaml", "output_node": "out"})

    def test_zero_step_rejected(self):
        with pytest.raises(ValidationError):
            iterate_loop.validate_config(
                {
                    "start": 0,
                    "end": 5,
                    "step": 0,
                    "pipeline_ref": "x.yaml",
                    "output_node": "out",
                }
            )

    def test_inverted_range_rejected_with_positive_step(self):
        with pytest.raises(ValidationError):
            iterate_loop.validate_config(
                {
                    "start": 5,
                    "end": 0,
                    "pipeline_ref": "x.yaml",
                    "output_node": "out",
                }
            )

    def test_negative_step_requires_decreasing_range(self):
        # Positive case: start > end with negative step is OK.
        iterate_loop.validate_config(
            {
                "start": 10,
                "end": 0,
                "step": -2,
                "pipeline_ref": "x.yaml",
                "output_node": "out",
            }
        )
        # Negative case: start < end with negative step rejected.
        with pytest.raises(ValidationError):
            iterate_loop.validate_config(
                {
                    "start": 0,
                    "end": 10,
                    "step": -2,
                    "pipeline_ref": "x.yaml",
                    "output_node": "out",
                }
            )


class TestIterateLoopApply:
    def test_iterates_over_range(self, tmp_path):
        for i in range(3):
            _make_csv(tmp_path, f"src-{i}.csv", f"id\n{i}\n")
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "mode: graph\n"
            "nodes:\n"
            "  - id: src\n"
            "    kind: source.file\n"
            "    config:\n"
            f"      path: {tmp_path}/src-{{{{iteration.value}}}}.csv\n"
            "edges: []\n",
            encoding="utf-8",
        )

        table = iterate_loop.apply(
            inputs=[],
            config={
                "start": 0,
                "end": 3,
                "pipeline_ref": str(sub_yaml),
                "output_node": "src",
            },
            ctx=None,
        )
        assert table.num_rows == 3
        assert table.column("id").to_pylist() == [0, 1, 2]


# ----- iterate_files -----------------------------------------------------


class TestIterateFilesValidation:
    def test_bad_source_class_path_rejected(self):
        with pytest.raises(ValidationError):
            iterate_files.validate_config(
                {
                    "source_class": "no_dot_in_path",
                    "source_config": {},
                    "pipeline_ref": "x.yaml",
                    "output_node": "out",
                }
            )

    def test_non_dict_source_config_rejected(self):
        with pytest.raises(ValidationError):
            iterate_files.validate_config(
                {
                    "source_class": "pkg.SomeSource",
                    "source_config": "not-a-dict",
                    "pipeline_ref": "x.yaml",
                    "output_node": "out",
                }
            )


class TestIterateFilesApply:
    """Use a stub FileSource module so we test the op without S3 / GCS / SFTP
    end-to-end (those have their own contract tests). The stub returns a
    fixed list of FileMeta; iterate_files should hit them in sorted order."""

    @pytest.fixture
    def stub_module(self, tmp_path, monkeypatch):
        """Build a tiny FileSource subclass + ConnectorConfig in a fake
        module and register it on sys.modules so importlib finds it."""
        import sys
        import types

        from decoy_engine.sdk import (
            CheckResult,
            ConnectorConfig,
            FileMeta,
            FileSource,
        )

        # Seed two files on disk so the sub-pipeline source.file can
        # actually read them.
        for name in ("alpha.csv", "beta.csv"):
            (tmp_path / name).write_text(f"id\n{name.split('.')[0]}\n", encoding="utf-8")

        class _StubConfig(ConnectorConfig):
            base: str

        class _StubFileSource(FileSource[_StubConfig]):
            name = "stub"
            version = "1.0.0"
            capabilities = {}

            def check(self):
                return CheckResult(ok=True)

            def list(self, prefix=None):
                # Yield both files. The op sorts by path so we deliberately
                # yield in reverse to exercise that.
                yield FileMeta(path=f"{self.config.base}/beta.csv", size=20)
                yield FileMeta(path=f"{self.config.base}/alpha.csv", size=10)

            def open(self, path):  # not used in this test
                yield b""

        mod = types.ModuleType("stub_pkg")
        mod._StubFileSource = _StubFileSource
        mod._StubConfig = _StubConfig
        monkeypatch.setitem(sys.modules, "stub_pkg", mod)
        return mod, tmp_path

    def test_iterates_over_listed_files_in_sorted_order(self, stub_module, tmp_path):
        _mod, base = stub_module
        # Sub-pipeline reads the file at the iteration path.
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "mode: graph\n"
            "nodes:\n"
            "  - id: src\n"
            "    kind: source.file\n"
            "    config:\n"
            "      path: '{{iteration.value}}'\n"
            "edges: []\n",
            encoding="utf-8",
        )

        table = iterate_files.apply(
            inputs=[],
            config={
                "source_class": "stub_pkg._StubFileSource",
                "source_config": {"base": str(base)},
                "pipeline_ref": str(sub_yaml),
                "output_node": "src",
            },
            ctx=None,
        )
        # Sorted order: alpha first, then beta.
        assert table.num_rows == 2
        assert table.column("id").to_pylist() == ["alpha", "beta"]

    def test_missing_source_class_raises_op_error(self, tmp_path):
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text("mode: graph\nnodes: []\nedges: []\n", encoding="utf-8")
        with pytest.raises(OpError, match="cannot import"):
            iterate_files.apply(
                inputs=[],
                config={
                    "source_class": "nonexistent_pkg.SomeSource",
                    "source_config": {},
                    "pipeline_ref": str(sub_yaml),
                    "output_node": "src",
                },
                ctx=None,
            )


# ----- Cross-cut: ops register in the dispatch ---------------------------


class TestOpsRegistry:
    def test_all_four_ops_registered(self):
        for kind in ("sub_pipeline", "iterate_fixed", "iterate_loop", "iterate_files"):
            assert kind in OPS, f"{kind!r} not in OPS dispatch"

    def test_ops_declare_required_metadata(self):
        for kind in ("sub_pipeline", "iterate_fixed", "iterate_loop", "iterate_files"):
            op = OPS[kind]
            assert hasattr(op, "KIND") and kind == op.KIND
            assert hasattr(op, "NATIVE_ENGINE")
            assert hasattr(op, "INPUT_ARITY")
            assert hasattr(op, "OUTPUT_KIND")
            assert hasattr(op, "validate_config")
            assert hasattr(op, "apply")
