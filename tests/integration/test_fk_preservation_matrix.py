"""Sprint 4 Commit 4: FK preservation four-case integration matrix.

Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG; materialize
parent pool; child samples with replacement.

End-to-end coverage of the four FK case-pairings declared in the
Sprint 4 plan:

  Case 1: mask1.pk -> mask2.fk
      Both columns mask deterministically. Same plaintext masks to
      same ciphertext via the literal `derive_key("mask")` info
      string, so every child FK value resolves to a matching parent
      PK value. No FK-preservation machinery is invoked at runtime
      (no pool_resolver call); the validator (Commit 3) is the only
      enforcement surface.

  Case 2: mask1.pk -> synth1.fk
      Parent masks PK column; synth child draws FK from the masked
      pool. generate_op.apply coerces the child strategy to
      `reference`, calls ctx.pool_resolver(parent_node, column), and
      hands the distinct values to ColumnGenerator's reference
      strategy via reference_data.

  Case 3: mask2.pk -> synth1.fk
      Same runtime path as case 2; parent_node is a downstream mask
      rather than an upstream one. Cache pinning keeps the parent
      alive past its normal consumer count.

  Case 4: synth1.pk -> synth2.fk
      Synth parent emits PK column; synth child draws FK from it.
      Same runtime path; topology ensures synth1 runs before synth2.

For each case the test:
  1. Builds a YAML graph (FK declared in column_relationships).
  2. Runs the graph end-to-end via execute_graph_capture (keeps
     parent + child cache entries reachable for assertion).
  3. Asserts the child column's distinct values are a strict subset
     of the parent column's distinct values (referential integrity).
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest
import yaml as _yaml

from decoy_engine.context import ExecutionContext
from decoy_engine.graph.runner import execute_graph_capture


def _y(cfg: dict) -> str:
    return _yaml.safe_dump(cfg, sort_keys=False)


@pytest.fixture
def parent_csv(tmp_path):
    """A small parent CSV with a unique PK column + a noise column."""
    src = tmp_path / "parent.csv"
    pd.DataFrame(
        {
            "id": ["alice", "bob", "carol", "dave", "eve"],
            "noise": [1, 2, 3, 4, 5],
        }
    ).to_csv(src, index=False)
    return str(src)


@pytest.fixture
def child_csv(tmp_path):
    """A child CSV whose customer_id column references parent.id."""
    src = tmp_path / "child.csv"
    pd.DataFrame(
        {
            "customer_id": ["alice", "bob", "alice", "carol", "bob", "dave"],
            "amount": [100, 200, 150, 300, 250, 175],
        }
    ).to_csv(src, index=False)
    return str(src)


@pytest.fixture
def out_dir(tmp_path):
    od = tmp_path / "out"
    od.mkdir()
    return str(od)


def _column_values(table_or_df, column: str) -> list:
    """Return the (non-null) values of a column from either an Arrow
    Table or a pandas DataFrame returned by the runner's cache."""
    if isinstance(table_or_df, pa.Table):
        col = table_or_df.column(column)
        return [v for v in col.to_pylist() if v is not None]
    if isinstance(table_or_df, pd.DataFrame):
        return [v for v in table_or_df[column].dropna().tolist()]
    return list(table_or_df)


class TestCase1MaskToMask:
    """Pattern: SDV deterministic-transform mode. Both columns mask
    with the same key + the same plaintext, so the FK survives by
    construction. No runtime pool_resolver call."""

    def test_mask_to_mask_fk_stable(self, parent_csv, child_csv, out_dir, tmp_path):
        cfg = {
            "mode": "graph",
            "nodes": [
                {
                    "id": "src_1",
                    "kind": "source.file",
                    "config": {"path": parent_csv, "format": "csv"},
                },
                {
                    "id": "mask_1",
                    "kind": "mask",
                    "config": {"columns": {"id": {"strategy": "hash"}}},
                },
                {
                    "id": "tgt_1",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "p_out.csv"), "format": "csv"},
                },
                {
                    "id": "src_2",
                    "kind": "source.file",
                    "config": {"path": child_csv, "format": "csv"},
                },
                {
                    "id": "mask_2",
                    "kind": "mask",
                    "config": {"columns": {"customer_id": {"strategy": "hash"}}},
                },
                {
                    "id": "tgt_2",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "c_out.csv"), "format": "csv"},
                },
            ],
            "edges": [
                {"from": "src_1", "to": "mask_1"},
                {"from": "mask_1", "to": "tgt_1"},
                {"from": "src_2", "to": "mask_2"},
                {"from": "mask_2", "to": "tgt_2"},
            ],
            "column_relationships": [
                {
                    "kind": "fk",
                    "parent": {"node": "mask_1", "column": "id"},
                    "child": {"node": "mask_2", "column": "customer_id"},
                },
            ],
        }
        # Master key bound via context.make_key_resolver so both
        # masks derive from the same root.
        from decoy_engine.context import make_key_resolver

        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "test-pipeline"),
        )
        result, cache = execute_graph_capture(
            _y(cfg),
            ctx=ctx,
            keep_nodes=["mask_1", "mask_2"],
        )
        assert result["success"], "run failed"

        parent_pks = set(_column_values(cache["mask_1"], "id"))
        child_fks = set(_column_values(cache["mask_2"], "customer_id"))

        # Referential integrity: every child FK exists in the parent PK pool.
        assert child_fks.issubset(parent_pks), (
            f"child FKs {child_fks - parent_pks} not present in parent PKs"
        )
        # The 4 distinct customer_ids in the child input ('alice', 'bob',
        # 'carol', 'dave') all map to one of the 5 parent ids.
        assert len(child_fks) == 4


class TestCase2MaskToSynth:
    """Pattern: SDV HMA1 parent-first + materialize pool + sample
    with replacement. The synth child column gets coerced to
    `reference`; pool_resolver hands it the masked parent's distinct
    values."""

    def test_mask_to_synth_fk_drawn_from_masked_pool(self, parent_csv, out_dir, tmp_path):
        cfg = {
            "mode": "graph",
            "nodes": [
                {
                    "id": "src_1",
                    "kind": "source.file",
                    "config": {"path": parent_csv, "format": "csv"},
                },
                {
                    "id": "mask_1",
                    "kind": "mask",
                    "config": {"columns": {"id": {"strategy": "hash"}}},
                },
                {
                    "id": "tgt_1",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "p_out.csv"), "format": "csv"},
                },
                # Synth child takes mask_1 as upstream. The edge
                # mask_1 -> synth_1 forces topo ordering (parent
                # before child) AND hands the child its row count
                # via generate_op's len(upstream) path. FK declaration
                # tells generate_op to coerce customer_id to
                # `reference` and populate the pool from mask_1.id.
                {
                    "id": "synth_1",
                    "kind": "generate",
                    "config": {
                        "columns": {
                            "customer_id": {"strategy": "faker", "faker_type": "name"},
                            "amount": {"strategy": "sequence", "start": 100},
                        },
                    },
                },
                {
                    "id": "tgt_2",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "c_out.csv"), "format": "csv"},
                },
            ],
            "edges": [
                {"from": "src_1", "to": "mask_1"},
                # mask_1 fans out: target write + synth child.
                {"from": "mask_1", "to": "tgt_1"},
                {"from": "mask_1", "to": "synth_1"},
                {"from": "synth_1", "to": "tgt_2"},
            ],
            "column_relationships": [
                {
                    "kind": "fk",
                    "parent": {"node": "mask_1", "column": "id"},
                    "child": {"node": "synth_1", "column": "customer_id"},
                },
            ],
        }
        from decoy_engine.context import make_key_resolver

        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "test-pipeline"),
            pipeline_derive_key=make_key_resolver(b"\x33" * 32, "test-pipeline"),
        )
        result, cache = execute_graph_capture(
            _y(cfg),
            ctx=ctx,
            keep_nodes=["mask_1", "synth_1"],
        )
        assert result["success"], "run failed"

        parent_pks = set(_column_values(cache["mask_1"], "id"))
        child_fks = set(_column_values(cache["synth_1"], "customer_id"))

        # Every synth child FK value is drawn from the masked parent
        # pool. With 20 child rows and 5 parent rows, all 5 parent
        # values may appear (sampling with replacement).
        assert child_fks.issubset(parent_pks), (
            f"child FKs {child_fks - parent_pks} not in parent PKs"
        )
        assert len(child_fks) > 0


class TestCase3MaskDownstreamToSynth:
    """Same runtime path as Case 2; differs in that the parent is
    the output of a longer mask chain rather than the first mask.
    Verifies that cache pinning keeps the intermediate parent alive
    past its single normal consumer (the target write)."""

    def test_mask_downstream_to_synth_fk(self, parent_csv, out_dir, tmp_path):
        cfg = {
            "mode": "graph",
            "nodes": [
                {
                    "id": "src_1",
                    "kind": "source.file",
                    "config": {"path": parent_csv, "format": "csv"},
                },
                # First mask hashes id (pass-through on the column shape).
                {
                    "id": "mask_1",
                    "kind": "mask",
                    "config": {"columns": {"id": {"strategy": "hash"}}},
                },
                # Second mask reads mask_1's output and applies a
                # date_shift to noise (different deterministic
                # strategy). id passes through unchanged.
                {
                    "id": "mask_2",
                    "kind": "mask",
                    "config": {"columns": {"id": {"strategy": "hash"}}},
                },
                {
                    "id": "tgt_1",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "p_out.csv"), "format": "csv"},
                },
                # Synth child reads from mask_2 (downstream parent).
                # Edge mask_2 -> synth_1 forces parent-first ordering.
                {
                    "id": "synth_1",
                    "kind": "generate",
                    "config": {
                        "columns": {
                            "customer_id": {"strategy": "faker", "faker_type": "name"},
                        },
                    },
                },
                {
                    "id": "tgt_2",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "c_out.csv"), "format": "csv"},
                },
            ],
            "edges": [
                {"from": "src_1", "to": "mask_1"},
                {"from": "mask_1", "to": "mask_2"},
                {"from": "mask_2", "to": "tgt_1"},
                {"from": "mask_2", "to": "synth_1"},
                {"from": "synth_1", "to": "tgt_2"},
            ],
            "column_relationships": [
                {
                    "kind": "fk",
                    "parent": {"node": "mask_2", "column": "id"},
                    "child": {"node": "synth_1", "column": "customer_id"},
                },
            ],
        }
        from decoy_engine.context import make_key_resolver

        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "test-pipeline"),
            pipeline_derive_key=make_key_resolver(b"\x33" * 32, "test-pipeline"),
        )
        result, cache = execute_graph_capture(
            _y(cfg),
            ctx=ctx,
            keep_nodes=["mask_2", "synth_1"],
        )
        assert result["success"], "run failed"

        parent_pks = set(_column_values(cache["mask_2"], "id"))
        child_fks = set(_column_values(cache["synth_1"], "customer_id"))
        assert child_fks.issubset(parent_pks)


class TestCase4SynthToSynth:
    """Parent and child are both pure generate ops. Parent emits a
    PK via a sequence strategy; child consumes via reference."""

    def test_synth_to_synth_fk(self, tmp_path):
        cfg = {
            "mode": "graph",
            "nodes": [
                {
                    "id": "synth_1",
                    "kind": "generate",
                    "config": {
                        "row_count": 8,
                        "columns": {
                            "user_id": {"strategy": "sequence", "start": 1000},
                        },
                    },
                },
                {
                    "id": "tgt_1",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "p_out.csv"), "format": "csv"},
                },
                {
                    "id": "synth_2",
                    "kind": "generate",
                    "config": {
                        "row_count": 15,
                        "columns": {
                            "fk_user_id": {"strategy": "faker", "faker_type": "name"},
                        },
                    },
                },
                {
                    "id": "tgt_2",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "c_out.csv"), "format": "csv"},
                },
            ],
            "edges": [
                {"from": "synth_1", "to": "tgt_1"},
                {"from": "synth_2", "to": "tgt_2"},
            ],
            "column_relationships": [
                {
                    "kind": "fk",
                    "parent": {"node": "synth_1", "column": "user_id"},
                    "child": {"node": "synth_2", "column": "fk_user_id"},
                },
            ],
        }
        from decoy_engine.context import make_key_resolver

        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "test-pipeline"),
            pipeline_derive_key=make_key_resolver(b"\x33" * 32, "test-pipeline"),
        )
        result, cache = execute_graph_capture(
            _y(cfg),
            ctx=ctx,
            keep_nodes=["synth_1", "synth_2"],
        )
        assert result["success"], "run failed"

        parent_pks = set(_column_values(cache["synth_1"], "user_id"))
        child_fks = set(_column_values(cache["synth_2"], "fk_user_id"))
        assert child_fks.issubset(parent_pks), f"child FKs not in parent: {child_fks - parent_pks}"


class TestCoercionAdvisory:
    """Verifies the runtime coercion path: the operator wrote
    strategy: faker on the FK child column; runtime coercion flipped
    it to reference. Exported in fk_preservation metrics so the
    manifest assembler can record the coercion."""

    def test_strategy_coerced_exported(self, parent_csv, tmp_path):
        cfg = {
            "mode": "graph",
            "nodes": [
                {
                    "id": "src_1",
                    "kind": "source.file",
                    "config": {"path": parent_csv, "format": "csv"},
                },
                {
                    "id": "mask_1",
                    "kind": "mask",
                    "config": {"columns": {"id": {"strategy": "hash"}}},
                },
                {
                    "id": "tgt_1",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "p_out.csv"), "format": "csv"},
                },
                {
                    "id": "synth_1",
                    "kind": "generate",
                    "config": {
                        # Note: faker, not reference. Coercion flips it.
                        "columns": {"customer_id": {"strategy": "faker"}},
                    },
                },
                {
                    "id": "tgt_2",
                    "kind": "target.file",
                    "config": {"output_filename": str(tmp_path / "c_out.csv"), "format": "csv"},
                },
            ],
            "edges": [
                {"from": "src_1", "to": "mask_1"},
                # mask_1 fans out: target write + synth child.
                {"from": "mask_1", "to": "tgt_1"},
                {"from": "mask_1", "to": "synth_1"},
                {"from": "synth_1", "to": "tgt_2"},
            ],
            "column_relationships": [
                {
                    "kind": "fk",
                    "parent": {"node": "mask_1", "column": "id"},
                    "child": {"node": "synth_1", "column": "customer_id"},
                },
            ],
        }
        from decoy_engine.context import make_key_resolver

        ctx = ExecutionContext(
            derive_key=make_key_resolver(b"\x42" * 32, "test-pipeline"),
            pipeline_derive_key=make_key_resolver(b"\x33" * 32, "test-pipeline"),
        )
        result, _cache = execute_graph_capture(
            _y(cfg),
            ctx=ctx,
            keep_nodes=["mask_1", "synth_1"],
        )
        assert result["success"]
        # Inspect the exports for synth_1 -- fk_preservation key should
        # carry coercion info for customer_id.
        synth_exports = ctx._exports.get("synth_1", {})
        assert "fk_preservation" in synth_exports
        fk_info = synth_exports["fk_preservation"]
        assert "customer_id" in fk_info
        assert fk_info["customer_id"]["strategy_coerced"] is True
        assert fk_info["customer_id"]["original_strategy"] == "faker"
        assert fk_info["customer_id"]["pool_size"] == 5  # parent has 5 unique ids
