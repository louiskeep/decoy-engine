"""Sprint 4 Commit 3: column_relationships validation stage.

Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG; materialize
parent pool; child samples with replacement.

Tests the new validation stage added to validate_graph_full that
checks the top-level column_relationships: block emitted by the
platform's FORECAST pipeline build.

Covers all FK_* codes added to validation_result.CODES:
  - FK_UNKNOWN_NODE: parent or child node not present in graph.
  - FK_UNKNOWN_COLUMN: column not declared in node config.
  - FK_PARENT_AFTER_CHILD: parent appears after child in topo order.
  - FK_SELF_REFERENCE: parent.node == child.node (V2 lifts).
  - FK_PARALLEL_BRANCHES: no topo path between parent + child;
    advisory in lenient, error in strict.
  - FK_NONDETERMINISTIC_MASK: mask op on FK uses non-deterministic
    strategy (redact, shuffle, truncate).

Each test composes a minimal YAML graph + an FK declaration and
asserts the codes that fire.
"""
from __future__ import annotations

import textwrap

import pytest

from decoy_engine.graph.runner import validate_graph_full
from decoy_engine.validation_result import CODES


def _base_two_table_graph(
    *,
    parent_mask_strategy: str = "hash",
    child_mask_strategy: str = "hash",
) -> str:
    """Minimal two-table graph with a declared FK between mask_1.id
    and mask_2.customer_id. Mirrors the customers + orders pattern.
    Branches are parallel (independent source files joined only by the
    declared FK), which is the realistic multi-table pipeline shape;
    cache pinning keeps both parents alive at runtime."""
    return textwrap.dedent(f"""
        mode: graph
        nodes:
          - id: src_1
            kind: source.file
            config: {{path: parent.csv, format: csv}}
          - id: mask_1
            kind: mask
            config: {{columns: {{id: {{strategy: {parent_mask_strategy}}}}}}}
          - id: tgt_1
            kind: target.file
            config: {{output_filename: p.csv, format: csv}}
          - id: src_2
            kind: source.file
            config: {{path: child.csv, format: csv}}
          - id: mask_2
            kind: mask
            config: {{columns: {{customer_id: {{strategy: {child_mask_strategy}}}}}}}
          - id: tgt_2
            kind: target.file
            config: {{output_filename: c.csv, format: csv}}
        edges:
          - {{from: src_1, to: mask_1}}
          - {{from: mask_1, to: tgt_1}}
          - {{from: src_2, to: mask_2}}
          - {{from: mask_2, to: tgt_2}}
        column_relationships:
          - kind: fk
            parent: {{node: mask_1, column: id}}
            child: {{node: mask_2, column: customer_id}}
    """).strip()


def _chain_three_nodes_graph() -> str:
    """Single-chain graph where mask_2 directly consumes mask_1's
    output. Used to verify the connected-branches case where no
    parallel-branches advisory fires."""
    return textwrap.dedent("""
        mode: graph
        nodes:
          - id: src_1
            kind: source.file
            config: {path: in.csv, format: csv}
          - id: mask_1
            kind: mask
            config: {columns: {id: {strategy: hash}}}
          - id: mask_2
            kind: mask
            config: {columns: {id: {strategy: hash}}}
          - id: tgt_1
            kind: target.file
            config: {output_filename: out.csv, format: csv}
        edges:
          - {from: src_1, to: mask_1}
          - {from: mask_1, to: mask_2}
          - {from: mask_2, to: tgt_1}
        column_relationships:
          - kind: fk
            parent: {node: mask_1, column: id}
            child: {node: mask_2, column: id}
    """).strip()


class TestHappyPath:
    def test_valid_fk_emits_only_advisory(self):
        """Two parallel mask branches with an FK = lenient mode emits
        fk.parallel_branches as a warning (advisory). No errors."""
        res = validate_graph_full(_base_two_table_graph())
        assert res.errors == []
        assert any(w.code == CODES.FK_PARALLEL_BRANCHES for w in res.warnings)
        assert res.ok is True

    def test_connected_chain_no_advisory(self):
        """When parent and child sit on a connected chain
        (src -> mask_1 -> mask_2 -> tgt), no parallel-branches
        advisory fires because the topology already enforces
        ordering."""
        res = validate_graph_full(_chain_three_nodes_graph())
        assert res.errors == []
        assert not any(w.code == CODES.FK_PARALLEL_BRANCHES for w in res.warnings)


class TestUnknownNode:
    def test_parent_node_missing(self):
        yaml = _base_two_table_graph().replace(
            "node: mask_1", "node: nonexistent_parent"
        )
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        assert CODES.FK_UNKNOWN_NODE in codes

    def test_child_node_missing(self):
        yaml = _base_two_table_graph().replace(
            "node: mask_2", "node: nonexistent_child"
        )
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        assert CODES.FK_UNKNOWN_NODE in codes


class TestUnknownColumn:
    def test_child_column_not_in_node_config(self):
        # Child mask declares column "customer_id" but FK references "ghost".
        yaml = _base_two_table_graph().replace(
            "child: {node: mask_2, column: customer_id}",
            "child: {node: mask_2, column: ghost}",
        )
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        assert CODES.FK_UNKNOWN_COLUMN in codes

    def test_source_node_column_gets_carve_out(self):
        """Source nodes don't carry a columns config (their schema is
        the file's). Validator must not reject an FK pointing at a
        source-side column."""
        yaml = _base_two_table_graph().replace(
            "parent: {node: mask_1, column: id}",
            "parent: {node: src_1, column: any_column}",
        )
        res = validate_graph_full(yaml)
        # FK_UNKNOWN_COLUMN must NOT fire for the source-side reference.
        assert not any(
            e.code == CODES.FK_UNKNOWN_COLUMN
            and "parent column" in e.message
            for e in res.errors
        )


class TestParentAfterChild:
    def test_parent_topologically_after_child_rejected(self):
        # Swap roles so the FK declares mask_2 as parent of mask_1.
        yaml = _base_two_table_graph().replace(
            "parent: {node: mask_1, column: id}",
            "parent: {node: mask_2, column: customer_id}",
        ).replace(
            "child: {node: mask_2, column: customer_id}",
            "child: {node: mask_1, column: id}",
        )
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        # mask_1 and mask_2 sit at the same topo position in parallel
        # branches; with `>= ` we treat that as parent-not-strictly-
        # before-child and emit the parent_after_child error.
        assert CODES.FK_PARENT_AFTER_CHILD in codes


class TestSelfReference:
    def test_self_reference_rejected_in_v1(self):
        yaml = _base_two_table_graph().replace(
            "child: {node: mask_2, column: customer_id}",
            "child: {node: mask_1, column: id}",
        )
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        assert CODES.FK_SELF_REFERENCE in codes


class TestParallelBranchesStrictMode:
    def test_strict_promotes_to_error(self):
        res = validate_graph_full(_base_two_table_graph(), strict=True)
        codes = [e.code for e in res.errors]
        # In strict mode the same parallel-branches case becomes an error.
        assert CODES.FK_PARALLEL_BRANCHES in codes

    def test_lenient_keeps_as_warning(self):
        res = validate_graph_full(_base_two_table_graph(), strict=False)
        # Errors must NOT include FK_PARALLEL_BRANCHES; warnings should.
        assert not any(e.code == CODES.FK_PARALLEL_BRANCHES for e in res.errors)
        assert any(w.code == CODES.FK_PARALLEL_BRANCHES for w in res.warnings)


class TestNondeterministicMask:
    @pytest.mark.parametrize("bad_strategy", ["redact", "shuffle", "truncate", "passthrough"])
    def test_nondeterministic_strategy_advisory_by_default(self, bad_strategy, monkeypatch):
        # Sprint A.3 of fk-preservation plan: the gate softens from
        # hard-reject to advisory by default so one-off scrubs with
        # redact / shuffle on an FK column don't hard-fail. The
        # message still fires; severity moves from error -> warning.
        monkeypatch.delenv("DECOY_FK_STRICT_DETERMINISM", raising=False)
        yaml = _base_two_table_graph(parent_mask_strategy=bad_strategy)
        res = validate_graph_full(yaml)
        error_codes = [e.code for e in res.errors]
        warning_codes = [w.code for w in res.warnings]
        assert CODES.FK_NONDETERMINISTIC_MASK not in error_codes, (
            f"strategy {bad_strategy!r} should now be advisory, not error"
        )
        assert CODES.FK_NONDETERMINISTIC_MASK in warning_codes, (
            f"strategy {bad_strategy!r} must still surface the advisory"
        )

    @pytest.mark.parametrize("bad_strategy", ["redact", "shuffle", "truncate"])
    def test_nondeterministic_strategy_strict_env_blocks(self, bad_strategy, monkeypatch):
        # When the operator opts back in to strict determinism via
        # DECOY_FK_STRICT_DETERMINISM=1, the gate restores hard-reject
        # behavior for the same strategies.
        monkeypatch.setenv("DECOY_FK_STRICT_DETERMINISM", "1")
        yaml = _base_two_table_graph(parent_mask_strategy=bad_strategy)
        res = validate_graph_full(yaml)
        error_codes = [e.code for e in res.errors]
        assert CODES.FK_NONDETERMINISTIC_MASK in error_codes, (
            f"strategy {bad_strategy!r} must hard-reject under strict env"
        )

    @pytest.mark.parametrize("good_strategy", ["hash", "fpe", "faker", "date_shift", "reference"])
    def test_deterministic_strategy_on_fk_accepted(self, good_strategy):
        yaml = _base_two_table_graph(
            parent_mask_strategy=good_strategy,
            child_mask_strategy=good_strategy,
        )
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        warning_codes = [w.code for w in res.warnings]
        # Neither an error nor an advisory should fire when the strategy
        # is deterministic — the FK roundtrips cleanly.
        assert CODES.FK_NONDETERMINISTIC_MASK not in codes
        assert CODES.FK_NONDETERMINISTIC_MASK not in warning_codes


class TestNoBlockNoOp:
    def test_pipeline_without_fk_block_validates_normally(self):
        yaml = textwrap.dedent("""
            mode: graph
            nodes:
              - id: src_1
                kind: source.file
                config: {path: a.csv, format: csv}
              - id: tgt_1
                kind: target.file
                config: {output_filename: a_out.csv, format: csv}
            edges:
              - {from: src_1, to: tgt_1}
        """).strip()
        res = validate_graph_full(yaml)
        # No column_relationships block; no FK checks fire.
        assert not any(e.code.startswith("fk.") for e in res.errors)
        assert not any(w.code.startswith("fk.") for w in res.warnings)
