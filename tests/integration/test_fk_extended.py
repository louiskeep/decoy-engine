"""Extended FK preservation: self-reference + many-to-many + multi-parent.

Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG; materialize
parent pool; child samples with replacement. This file extends the
four-case matrix in test_fk_preservation_matrix.py with the new FK
kinds shipped after Sprint 4:

  Self-reference (employees.manager_id -> employees.id)
    Same node on both sides; engine reads parent pool from the
    in-flight `out` DataFrame instead of pool_resolver. Two-pass
    column iteration in generate_op.apply.

  Many-to-many junction (enrollments students <-> courses)
    `kind: m2m` writes both junction columns at once by sampling
    (left, right) pairs from the two parent pools per pool_strategy
    (cartesian | sampled).

  Multi-parent FK (composite-key references)
    `parent: [...]` array form. The child column draws a composite
    "left|right|..." string from the joint distribution of parents.

  PK uniqueness post-pass
    Columns marked `primary_key: true` are scanned for duplicates;
    the metric exports to ctx so the manifest assembler hydrates
    the pk_uniqueness section.
"""
from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest
import yaml as _yaml

from decoy_engine.context import ExecutionContext, make_key_resolver
from decoy_engine.graph.runner import execute_graph_capture


def _y(cfg: dict) -> str:
    return _yaml.safe_dump(cfg, sort_keys=False)


def _column_values(table_or_df, column: str) -> list:
    if isinstance(table_or_df, pa.Table):
        return [v for v in table_or_df.column(column).to_pylist() if v is not None]
    if isinstance(table_or_df, pd.DataFrame):
        return [v for v in table_or_df[column].dropna().tolist()]
    return list(table_or_df)


class TestSelfReference:
    """employees.manager_id -> employees.id within a single generate op."""

    def test_self_ref_child_subset_of_parent(self, tmp_path):
        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "synth_emp", "kind": "generate",
                 "config": {
                     "row_count": 10,
                     "columns": {
                         "id":         {"strategy": "sequence", "start": 1000},
                         "manager_id": {"strategy": "faker", "faker_type": "word"},
                     },
                 }},
                {"id": "tgt", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "emp.csv"),
                            "format": "csv"}},
            ],
            "edges": [
                {"from": "synth_emp", "to": "tgt"},
            ],
            "column_relationships": [
                {"kind": "fk",
                 "parent": {"node": "synth_emp", "column": "id"},
                 "child":  {"node": "synth_emp", "column": "manager_id"}},
            ],
        }
        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "test-pipeline"),
            pipeline_derive_key=make_key_resolver(b"\x33" * 32, "test-pipeline"),
        )
        result, cache = execute_graph_capture(
            _y(cfg), ctx=ctx, keep_nodes=["synth_emp"],
        )
        assert result["success"], f"run failed: {result}"
        ids = set(_column_values(cache["synth_emp"], "id"))
        mids = set(_column_values(cache["synth_emp"], "manager_id"))
        # Every manager_id must be one of the produced ids (self-ref FK).
        assert mids.issubset(ids), (
            f"self-ref violation: manager_ids not in ids: {mids - ids}"
        )

    def test_self_ref_deterministic_with_same_key(self, tmp_path):
        """Same pipeline key -> same per-row manager assignment."""
        def _build_cfg(out_path):
            return {
                "mode": "graph",
                "nodes": [
                    {"id": "synth_emp", "kind": "generate",
                     "config": {
                         "row_count": 5,
                         "columns": {
                             "id":         {"strategy": "sequence", "start": 1},
                             "manager_id": {"strategy": "faker", "faker_type": "word"},
                         },
                     }},
                    {"id": "tgt", "kind": "target.file",
                     "config": {"output_filename": out_path, "format": "csv"}},
                ],
                "edges": [{"from": "synth_emp", "to": "tgt"}],
                "column_relationships": [
                    {"kind": "fk",
                     "parent": {"node": "synth_emp", "column": "id"},
                     "child":  {"node": "synth_emp", "column": "manager_id"}},
                ],
            }
        key = b"\x99" * 32
        runs = []
        for i in range(2):
            ctx = ExecutionContext(
                derive_key=make_key_resolver(key, "det-pipeline"),
                pipeline_derive_key=make_key_resolver(key, "det-pipeline"),
            )
            _, cache = execute_graph_capture(
                _y(_build_cfg(str(tmp_path / f"r{i}.csv"))),
                ctx=ctx, keep_nodes=["synth_emp"],
            )
            runs.append([
                (str(a), str(b)) for a, b in zip(
                    _column_values(cache["synth_emp"], "id"),
                    _column_values(cache["synth_emp"], "manager_id"),
                )
            ])
        assert runs[0] == runs[1], (
            "self-ref FK should be deterministic with the same pipeline key"
        )


class TestManyToMany:
    """enrollments junction (students.id, courses.id) via kind: m2m."""

    def test_cartesian_emits_full_product(self, tmp_path):
        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "students", "kind": "generate",
                 "config": {"row_count": 3, "columns": {
                     "id": {"strategy": "sequence", "start": 1},
                 }}},
                {"id": "courses", "kind": "generate",
                 "config": {"row_count": 4, "columns": {
                     "id": {"strategy": "sequence", "start": 100},
                 }}},
                {"id": "enrollments", "kind": "generate",
                 "config": {"row_count": 12, "columns": {
                     "student_id": {"strategy": "faker", "faker_type": "word"},
                     "course_id":  {"strategy": "faker", "faker_type": "word"},
                 }}},
                {"id": "tgt_s", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "s.csv"), "format": "csv"}},
                {"id": "tgt_c", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "c.csv"), "format": "csv"}},
                {"id": "tgt_e", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "e.csv"), "format": "csv"}},
            ],
            "edges": [
                {"from": "students", "to": "tgt_s"},
                {"from": "courses",  "to": "tgt_c"},
                {"from": "enrollments", "to": "tgt_e"},
            ],
            "column_relationships": [
                {"kind": "m2m",
                 "junction":    {"node": "enrollments", "columns": ["student_id", "course_id"]},
                 "left_parent":  {"node": "students", "column": "id"},
                 "right_parent": {"node": "courses",  "column": "id"},
                 "pool_strategy": "cartesian"},
            ],
        }
        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "m2m-test"),
            pipeline_derive_key=make_key_resolver(b"\x33" * 32, "m2m-test"),
        )
        result, cache = execute_graph_capture(
            _y(cfg), ctx=ctx,
            keep_nodes=["students", "courses", "enrollments"],
        )
        assert result["success"], f"m2m cartesian run failed: {result}"
        pairs = list(zip(
            _column_values(cache["enrollments"], "student_id"),
            _column_values(cache["enrollments"], "course_id"),
        ))
        # 3 students x 4 courses = 12 pairs.
        assert len(pairs) == 12, f"cartesian should emit 12 pairs, got {len(pairs)}"
        # Every pair is a unique (student, course) combination.
        assert len(set(pairs)) == 12, f"cartesian pairs must be distinct: {pairs}"

    def test_weighted_biases_pair_distribution(self, tmp_path):
        """Tier-4 #21 (2026-05-20): weighted pool_strategy honors
        per-side weights. With skewed right-side weights, the popular
        course should dominate the junction output."""
        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "students", "kind": "generate",
                 "config": {"row_count": 5, "columns": {
                     "id": {"strategy": "sequence", "start": 1},
                 }}},
                {"id": "courses", "kind": "generate",
                 "config": {"row_count": 3, "columns": {
                     "id": {"strategy": "sequence", "start": 100},
                 }}},
                {"id": "enrollments", "kind": "generate",
                 "config": {"row_count": 200, "columns": {
                     "student_id": {"strategy": "faker", "faker_type": "word"},
                     "course_id":  {"strategy": "faker", "faker_type": "word"},
                 }}},
                {"id": "tgt_s", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "s.csv"), "format": "csv"}},
                {"id": "tgt_c", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "c.csv"), "format": "csv"}},
                {"id": "tgt_e", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "e.csv"), "format": "csv"}},
            ],
            "edges": [
                {"from": "students", "to": "tgt_s"},
                {"from": "courses",  "to": "tgt_c"},
                {"from": "enrollments", "to": "tgt_e"},
            ],
            "column_relationships": [
                {"kind": "m2m",
                 "junction":    {"node": "enrollments", "columns": ["student_id", "course_id"]},
                 "left_parent":  {"node": "students", "column": "id"},
                 "right_parent": {"node": "courses",  "column": "id"},
                 "pool_strategy": "weighted",
                 # Right side: course 100 is the popular one (weight 95);
                 # 101 + 102 are unpopular (5 + 0). The output should be
                 # dominated by course_id=100.
                 "right_weights": [95.0, 5.0, 0.0],
                 # Left side: leave weights off -> uniform.
                },
            ],
        }
        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "m2m-weighted-test"),
            pipeline_derive_key=make_key_resolver(b"\x33" * 32, "m2m-weighted-test"),
        )
        result, cache = execute_graph_capture(
            _y(cfg), ctx=ctx,
            keep_nodes=["students", "courses", "enrollments"],
        )
        assert result["success"], f"m2m weighted run failed: {result}"
        from collections import Counter
        # Values in the cache round-trip as strings via the CSV target;
        # normalize to ints so the assertions don't depend on storage type.
        course_counts = Counter(
            int(v) for v in _column_values(cache["enrollments"], "course_id")
        )
        # Course 100 should dominate (weight 95 out of 100 total).
        assert course_counts[100] > course_counts[101] * 5, (
            f"weighted m2m didn't bias toward course 100: {course_counts}"
        )
        # Course 102 (weight 0) should never appear.
        assert course_counts.get(102, 0) == 0, (
            f"zero-weight course leaked into output: {course_counts}"
        )


class TestMultiParentFK:
    """Composite-key FK: parent: [...] array form."""

    def test_multi_parent_pool_built_from_zipped_parents(self, tmp_path):
        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "a", "kind": "generate",
                 "config": {"row_count": 5, "columns": {
                     "id": {"strategy": "sequence", "start": 1},
                 }}},
                {"id": "b", "kind": "generate",
                 "config": {"row_count": 5, "columns": {
                     "id": {"strategy": "sequence", "start": 100},
                 }}},
                {"id": "child", "kind": "generate",
                 "config": {"row_count": 8, "columns": {
                     "composite": {"strategy": "faker", "faker_type": "word"},
                 }}},
                {"id": "tgt_a", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "a.csv"), "format": "csv"}},
                {"id": "tgt_b", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "b.csv"), "format": "csv"}},
                {"id": "tgt_c", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "c.csv"), "format": "csv"}},
            ],
            "edges": [
                {"from": "a", "to": "tgt_a"},
                {"from": "b", "to": "tgt_b"},
                {"from": "child", "to": "tgt_c"},
            ],
            "column_relationships": [
                {"kind": "fk",
                 "parent": [
                     {"node": "a", "column": "id"},
                     {"node": "b", "column": "id"},
                 ],
                 "child": {"node": "child", "column": "composite"}},
            ],
        }
        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "multi-test"),
            pipeline_derive_key=make_key_resolver(b"\x33" * 32, "multi-test"),
        )
        result, cache = execute_graph_capture(
            _y(cfg), ctx=ctx, keep_nodes=["a", "b", "child"],
        )
        assert result["success"], f"multi-parent run failed: {result}"
        # Build the expected composite pool by zipping parent values.
        a_vals = _column_values(cache["a"], "id")
        b_vals = _column_values(cache["b"], "id")
        expected_pool = {f"{a}|{b}" for a, b in zip(a_vals, b_vals)}
        child_vals = set(_column_values(cache["child"], "composite"))
        # Every child value must be one of the zipped parent tuples.
        assert child_vals.issubset(expected_pool), (
            f"multi-parent violation: child values not in zipped pool: "
            f"{child_vals - expected_pool}"
        )


class TestPKUniqueness:
    """primary_key: true columns emit pk_uniqueness metric on duplicates."""

    def test_sequence_pk_has_no_duplicates(self, tmp_path):
        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "synth", "kind": "generate",
                 "config": {"row_count": 100, "columns": {
                     "id": {"strategy": "sequence", "start": 1, "primary_key": True},
                 }}},
                {"id": "tgt", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "u.csv"), "format": "csv"}},
            ],
            "edges": [{"from": "synth", "to": "tgt"}],
        }
        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "pk-test"),
            pipeline_derive_key=make_key_resolver(b"\x33" * 32, "pk-test"),
        )
        result, _ = execute_graph_capture(_y(cfg), ctx=ctx, keep_nodes=["synth"])
        assert result["success"]
        # Find the node record and inspect its pk_uniqueness export.
        synth_rec = next(
            r for r in result["nodes"] if r["node_id"] == "synth"
        )
        pk_metrics = synth_rec.get("exports", {}).get("pk_uniqueness", {})
        assert "id" in pk_metrics
        assert pk_metrics["id"]["duplicate_count"] == 0
        assert pk_metrics["id"]["unique_values"] == 100

    def test_categorical_pk_strict_default_fails(self, tmp_path, monkeypatch):
        """Tier-1 audit (2026-05-20): PK duplicates now error by default.
        A categorical strategy on a PK is guaranteed to collide when
        row_count > len(categories) — that should fail the run, not
        silently produce non-unique keys."""
        # Ensure no leftover env var from another test.
        monkeypatch.delenv("DECOY_PK_LENIENT", raising=False)
        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "synth", "kind": "generate",
                 "config": {"row_count": 50, "columns": {
                     "id": {
                         "strategy": "categorical",
                         "categories": ["A", "B", "C"],
                         "primary_key": True,
                     },
                 }}},
                {"id": "tgt", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "u.csv"), "format": "csv"}},
            ],
            "edges": [{"from": "synth", "to": "tgt"}],
        }
        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "pk-test"),
            pipeline_derive_key=make_key_resolver(b"\x33" * 32, "pk-test"),
        )
        result, _ = execute_graph_capture(_y(cfg), ctx=ctx, keep_nodes=["synth"])
        # Run should fail.
        assert not result["success"]
        # Inspect the failing node — its error_code should carry pk.duplicates
        # (translate_engine_error forwards the .code attribute set on
        # PKDuplicatesError onto the resulting OpError via
        # _forward_structured_metadata).
        synth_rec = next(r for r in result["nodes"] if r["node_id"] == "synth")
        assert synth_rec.get("error_code") == "pk.duplicates", (
            f"expected error_code pk.duplicates, got "
            f"error_code={synth_rec.get('error_code')!r} "
            f"error={synth_rec.get('error')!r}"
        )
        # Metric still exported despite the failure so auditors can see why.
        pk_metrics = synth_rec.get("exports", {}).get("pk_uniqueness", {})
        assert "id" in pk_metrics
        assert pk_metrics["id"]["duplicate_count"] > 0

    def test_categorical_pk_lenient_env_passes(self, tmp_path, monkeypatch):
        """DECOY_PK_LENIENT=1 reverts to the pre-audit behavior: log +
        manifest export, continue. Useful for one-off scrubs where the
        operator knows the collisions are tolerated."""
        monkeypatch.setenv("DECOY_PK_LENIENT", "1")
        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "synth", "kind": "generate",
                 "config": {"row_count": 50, "columns": {
                     "id": {
                         "strategy": "categorical",
                         "categories": ["A", "B", "C"],
                         "primary_key": True,
                     },
                 }}},
                {"id": "tgt", "kind": "target.file",
                 "config": {"output_filename": str(tmp_path / "u.csv"), "format": "csv"}},
            ],
            "edges": [{"from": "synth", "to": "tgt"}],
        }
        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "pk-test"),
            pipeline_derive_key=make_key_resolver(b"\x33" * 32, "pk-test"),
        )
        result, _ = execute_graph_capture(_y(cfg), ctx=ctx, keep_nodes=["synth"])
        assert result["success"]
        synth_rec = next(r for r in result["nodes"] if r["node_id"] == "synth")
        pk_metrics = synth_rec.get("exports", {}).get("pk_uniqueness", {})
        assert pk_metrics["id"]["duplicate_count"] > 0
