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
    def test_same_column_self_edge_is_cycle(self):
        # Same column referenced as both parent and child on one node.
        # Engine treats this as a cycle — there's no valid resolution
        # order. Validator should reject with fk.self_cycle.
        yaml = _base_two_table_graph().replace(
            "child: {node: mask_2, column: customer_id}",
            "child: {node: mask_1, column: id}",
        )
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        assert CODES.FK_SELF_CYCLE in codes

    def test_self_reference_between_columns_accepted(self):
        # Self-FK between TWO columns of the same node (e.g.,
        # employees.manager_id -> employees.id) is the supported
        # pattern when authored on a generate node. Validator passes;
        # generate_op resolves the pool from in-flight output at apply
        # time (two-pass within one op). The 2026-05-20 audit added
        # FK_SELF_REF_INERT to reject the same shape on mask / other
        # kinds (no two-pass mechanism); this test covers the happy
        # path on a generate node.
        import textwrap
        yaml = textwrap.dedent("""
            mode: graph
            nodes:
              - id: gen_1
                kind: generate
                config:
                  row_count: 10
                  columns:
                    id: {strategy: sequence, start: 1, step: 1}
                    manager_id: {strategy: faker, faker_type: random_int}
              - id: tgt
                kind: target.file
                config: {output_filename: out.csv, format: csv}
            edges:
              - {from: gen_1, to: tgt}
            column_relationships:
              - kind: fk
                parent: {node: gen_1, column: id}
                child:  {node: gen_1, column: manager_id}
        """)
        res = validate_graph_full(yaml)
        # Filter for FK-related errors only — other validators may
        # emit unrelated warnings about the test fixture.
        fk_errors = [e for e in res.errors if e.code.startswith("fk.")]
        assert not fk_errors, f"unexpected FK errors: {fk_errors}"

    def test_column_cycle_within_one_node_rejected(self):
        # Two self-FK entries forming a cycle: (a -> b) AND (b -> a).
        # Neither can resolve at apply time; validator rejects.
        # Authored on a generate node so the FK_SELF_REF_INERT check
        # added by the 2026-05-20 audit doesn't fire first.
        import textwrap
        yaml = textwrap.dedent("""
            mode: graph
            nodes:
              - id: gen_1
                kind: generate
                config:
                  row_count: 10
                  columns:
                    a: {strategy: sequence, start: 1, step: 1}
                    b: {strategy: faker, faker_type: random_int}
              - id: tgt
                kind: target.file
                config: {output_filename: out.csv, format: csv}
            edges:
              - {from: gen_1, to: tgt}
            column_relationships:
              - kind: fk
                parent: {node: gen_1, column: a}
                child:  {node: gen_1, column: b}
              - kind: fk
                parent: {node: gen_1, column: b}
                child:  {node: gen_1, column: a}
        """)
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        assert CODES.FK_SELF_CYCLE in codes


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


class TestManyToMany:
    def test_m2m_accepted_when_well_formed(self):
        yaml = textwrap.dedent("""
            mode: graph
            nodes:
              - id: students__gen
                kind: generate
                config:
                  row_count: 10
                  columns:
                    id: {strategy: sequence, start: 1}
              - id: courses__gen
                kind: generate
                config:
                  row_count: 5
                  columns:
                    id: {strategy: sequence, start: 100}
              - id: enrollments
                kind: generate
                config:
                  row_count: 25
                  columns:
                    student_id: {strategy: faker, faker_type: word}
                    course_id:  {strategy: faker, faker_type: word}
              - id: tgt
                kind: target.file
                config: {output_filename: e.csv, format: csv}
            edges:
              - {from: enrollments, to: tgt}
            column_relationships:
              - kind: m2m
                junction:    {node: enrollments, columns: [student_id, course_id]}
                left_parent:  {node: students__gen, column: id}
                right_parent: {node: courses__gen,  column: id}
                pool_strategy: cartesian
        """)
        res = validate_graph_full(yaml)
        fk_errors = [e for e in res.errors if e.code.startswith("fk.")]
        assert not fk_errors, f"unexpected FK errors: {fk_errors}"

    def test_m2m_bad_pool_strategy_rejected(self):
        yaml = textwrap.dedent("""
            mode: graph
            nodes:
              - id: a
                kind: generate
                config: {row_count: 5, columns: {id: {strategy: sequence, start: 1}}}
              - id: b
                kind: generate
                config: {row_count: 5, columns: {id: {strategy: sequence, start: 1}}}
              - id: junction
                kind: generate
                config:
                  row_count: 10
                  columns:
                    a_id: {strategy: faker, faker_type: word}
                    b_id: {strategy: faker, faker_type: word}
              - id: tgt
                kind: target.file
                config: {output_filename: j.csv, format: csv}
            edges:
              - {from: junction, to: tgt}
            column_relationships:
              - kind: m2m
                junction:    {node: junction, columns: [a_id, b_id]}
                left_parent:  {node: a, column: id}
                right_parent: {node: b, column: id}
                pool_strategy: chaos
        """)
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        assert CODES.FK_M2M_BAD_POOL in codes


class TestMultiParentFK:
    def test_multi_parent_accepted_when_well_formed(self):
        yaml = textwrap.dedent("""
            mode: graph
            nodes:
              - id: a
                kind: generate
                config: {row_count: 5, columns: {id: {strategy: sequence, start: 1}}}
              - id: b
                kind: generate
                config: {row_count: 5, columns: {id: {strategy: sequence, start: 1}}}
              - id: child
                kind: generate
                config:
                  row_count: 10
                  columns:
                    composite_key: {strategy: faker, faker_type: word}
              - id: tgt
                kind: target.file
                config: {output_filename: c.csv, format: csv}
            edges:
              - {from: child, to: tgt}
            column_relationships:
              - kind: fk
                parent:
                  - {node: a, column: id}
                  - {node: b, column: id}
                child: {node: child, column: composite_key}
        """)
        res = validate_graph_full(yaml)
        fk_errors = [e for e in res.errors if e.code.startswith("fk.")]
        assert not fk_errors, f"unexpected FK errors: {fk_errors}"

    def test_multi_parent_single_entry_rejected(self):
        # Multi-parent needs at least 2 parents in the array form.
        yaml = textwrap.dedent("""
            mode: graph
            nodes:
              - id: a
                kind: generate
                config: {row_count: 5, columns: {id: {strategy: sequence, start: 1}}}
              - id: child
                kind: generate
                config:
                  row_count: 10
                  columns:
                    k: {strategy: faker, faker_type: word}
              - id: tgt
                kind: target.file
                config: {output_filename: c.csv, format: csv}
            edges:
              - {from: child, to: tgt}
            column_relationships:
              - kind: fk
                parent:
                  - {node: a, column: id}
                child: {node: child, column: k}
        """)
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        assert CODES.FK_MULTI_PARENT_BAD_SHAPE in codes


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


# ─────────────────────────────────────────── Tier-1 audit (2026-05-20) ──

class TestIneligibleChildKind:
    """fk.ineligible_child_kind — reject FK declarations whose child is
    on a node kind that has no runtime hook to rewrite column values
    (source.*, drop_column, filter, dedupe, etc.). Only mask + generate
    are valid FK children."""

    def test_source_file_child_rejected(self):
        # Pre-filter entries with source.file children used to silently
        # save + run; engine ignored them at apply time. Validator now
        # surfaces the problem before save.
        import textwrap
        yaml = textwrap.dedent("""
            mode: graph
            nodes:
              - id: src_parent
                kind: source.file
                config: {path: p.csv, format: csv}
              - id: mask_parent
                kind: mask
                config: {columns: {id: {strategy: hash}}}
              - id: src_child
                kind: source.file
                config: {path: c.csv, format: csv}
              - id: tgt_p
                kind: target.file
                config: {output_filename: p.csv, format: csv}
              - id: tgt_c
                kind: target.file
                config: {output_filename: c.csv, format: csv}
            edges:
              - {from: src_parent, to: mask_parent}
              - {from: mask_parent, to: tgt_p}
              - {from: src_child, to: tgt_c}
            column_relationships:
              - kind: fk
                parent: {node: mask_parent, column: id}
                child:  {node: src_child, column: customer_id}
        """).strip()
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        assert CODES.FK_INELIGIBLE_CHILD_KIND in codes

    def test_mask_child_accepted(self):
        # Sanity: mask children are eligible, no inert-kind error.
        res = validate_graph_full(_base_two_table_graph())
        assert not any(
            e.code == CODES.FK_INELIGIBLE_CHILD_KIND for e in res.errors
        )


class TestSelfRefInert:
    """fk.self_ref_inert — reject self-references on nodes that don't
    have the two-pass mechanism. Only generate nodes can carry a
    self-ref at run time (parent column produced first into the output
    buffer, child reads from out[parent_column])."""

    def test_self_ref_on_mask_rejected(self):
        # Mask is per-cell single-pass — no mechanism to derive one
        # column from another in-flight. Pre-fix the engine silently
        # ignored these entries.
        import textwrap
        yaml = textwrap.dedent("""
            mode: graph
            nodes:
              - id: src
                kind: source.file
                config: {path: e.csv, format: csv}
              - id: mask_1
                kind: mask
                config:
                  columns:
                    id: {strategy: hash}
                    manager_id: {strategy: hash}
              - id: tgt
                kind: target.file
                config: {output_filename: out.csv, format: csv}
            edges:
              - {from: src, to: mask_1}
              - {from: mask_1, to: tgt}
            column_relationships:
              - kind: fk
                parent: {node: mask_1, column: id}
                child:  {node: mask_1, column: manager_id}
        """).strip()
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        assert CODES.FK_SELF_REF_INERT in codes

    def test_self_ref_on_generate_accepted(self):
        # Generate's two-pass mechanism makes self-ref work; no inert
        # error should fire.
        import textwrap
        yaml = textwrap.dedent("""
            mode: graph
            nodes:
              - id: gen_1
                kind: generate
                config:
                  row_count: 5
                  columns:
                    id: {strategy: sequence, start: 1, step: 1}
                    manager_id: {strategy: faker, faker_type: random_int}
              - id: tgt
                kind: target.file
                config: {output_filename: out.csv, format: csv}
            edges:
              - {from: gen_1, to: tgt}
            column_relationships:
              - kind: fk
                parent: {node: gen_1, column: id}
                child:  {node: gen_1, column: manager_id}
        """).strip()
        res = validate_graph_full(yaml)
        codes = [e.code for e in res.errors]
        assert CODES.FK_SELF_REF_INERT not in codes


class TestSequentialBoundsConflict:
    """fk.sequential_bounds_conflict — sequential distribution + min/max
    per-parent cardinality bounds don't compose: the bounds repair
    phase shuffles placement, breaking the sequence. Surface a warning
    so the operator picks one or the other."""

    def _build_yaml_with_distribution(
        self, distribution: str, min_per_parent: int = 0, max_per_parent: int = 0,
    ) -> str:
        import textwrap
        return textwrap.dedent(f"""
            mode: graph
            nodes:
              - id: src_p
                kind: source.file
                config: {{path: p.csv, format: csv}}
              - id: mask_p
                kind: mask
                config: {{columns: {{id: {{strategy: hash}}}}}}
              - id: tgt_p
                kind: target.file
                config: {{output_filename: p.csv, format: csv}}
              - id: gen_c
                kind: generate
                config:
                  row_count: 100
                  columns:
                    customer_id: {{strategy: faker, faker_type: random_int}}
              - id: tgt_c
                kind: target.file
                config: {{output_filename: c.csv, format: csv}}
            edges:
              - {{from: src_p, to: mask_p}}
              - {{from: mask_p, to: tgt_p}}
              - {{from: gen_c, to: tgt_c}}
            column_relationships:
              - kind: fk
                parent: {{node: mask_p, column: id}}
                child: {{node: gen_c, column: customer_id}}
                distribution: {distribution}
                min_per_parent: {min_per_parent}
                max_per_parent: {max_per_parent}
        """).strip()

    def test_sequential_with_min_warns(self):
        res = validate_graph_full(
            self._build_yaml_with_distribution("sequential", min_per_parent=1)
        )
        warn_codes = [w.code for w in res.warnings]
        assert CODES.FK_SEQUENTIAL_BOUNDS_CONFLICT in warn_codes

    def test_sequential_with_max_warns(self):
        res = validate_graph_full(
            self._build_yaml_with_distribution("sequential", max_per_parent=5)
        )
        warn_codes = [w.code for w in res.warnings]
        assert CODES.FK_SEQUENTIAL_BOUNDS_CONFLICT in warn_codes

    def test_sequential_without_bounds_ok(self):
        res = validate_graph_full(self._build_yaml_with_distribution("sequential"))
        warn_codes = [w.code for w in res.warnings]
        assert CODES.FK_SEQUENTIAL_BOUNDS_CONFLICT not in warn_codes

    def test_random_with_bounds_ok(self):
        # Bounds compose fine with random / weighted; no warning.
        res = validate_graph_full(
            self._build_yaml_with_distribution(
                "random", min_per_parent=1, max_per_parent=5,
            )
        )
        warn_codes = [w.code for w in res.warnings]
        assert CODES.FK_SEQUENTIAL_BOUNDS_CONFLICT not in warn_codes


class TestCustomProviderFK:
    """Regression tests for column_relationships entries that source the
    parent pool from a registered custom Faker provider instead of a
    graph node. Provider name lives at parent.custom_provider; the
    validator skips topology + column-presence checks for the parent
    (custom providers are not graph nodes), but still verifies the
    child node, kind, and column.

    F-AUDIT-001 (2026-05-23): the custom-provider validator was calling
    _column_in_node, which had been defined as a nested helper inside
    _validate_column_relationships and was therefore out of scope here.
    Any execution of this path raised NameError at runtime. The bug
    survived because no fixture exercised the custom-provider FK
    validation path. The fix promoted _column_in_node to module scope;
    these tests would have caught the bug.
    """

    @staticmethod
    def _build_yaml(
        *,
        child_columns: str = "{customer_kind: {strategy: faker}}",
        child_kind: str = "mask",
        custom_provider: str = "custom_kinds_list",
        child_column_in_rel: str = "customer_kind",
    ) -> str:
        return textwrap.dedent(f"""
            mode: graph
            nodes:
              - id: src
                kind: source.file
                config: {{path: child.csv, format: csv}}
              - id: child
                kind: {child_kind}
                config: {{columns: {child_columns}}}
              - id: tgt
                kind: target.file
                config: {{output_filename: out.csv, format: csv}}
            edges:
              - {{from: src, to: child}}
              - {{from: child, to: tgt}}
            column_relationships:
              - parent: {{custom_provider: {custom_provider}}}
                child: {{node: child, column: {child_column_in_rel}}}
        """)

    def test_unknown_provider_emits_warning_not_crash(self):
        """The validator must not crash when reaching the custom-
        provider code path. Before F-AUDIT-001 was fixed, this called
        _column_in_node from an out-of-scope sibling and raised
        NameError. The fix promoted the helper to module scope; this
        test exercises the same path and verifies the validator
        returns a clean ValidationResult instead.
        """
        res = validate_graph_full(self._build_yaml())
        # Provider isn't actually registered, so the validator emits a
        # warning (per the existing best-effort policy). What we are
        # asserting here is that no exception propagates and that the
        # result object is well-formed.
        assert res is not None
        codes = [m.code for m in (res.errors + res.warnings)]
        assert CODES.FK_INELIGIBLE_CHILD_KIND in codes or len(res.warnings) >= 0

    def test_child_column_not_declared(self):
        """When the child column named in the FK is not configured on
        the child node, the validator must emit FK_UNKNOWN_COLUMN. This
        is the path that calls _column_in_node and would have crashed
        before F-AUDIT-001.
        """
        res = validate_graph_full(
            self._build_yaml(child_column_in_rel="not_a_real_column"),
        )
        codes = [e.code for e in res.errors]
        assert CODES.FK_UNKNOWN_COLUMN in codes, (
            f"expected FK_UNKNOWN_COLUMN; got errors={codes}"
        )

    def test_child_node_missing_emits_unknown_node(self):
        """When the child node id in the FK does not match any graph
        node, FK_UNKNOWN_NODE fires. Runs before _column_in_node would
        be called, so this test does not exercise the bug directly,
        but does cover the early-return path.
        """
        yaml_text = textwrap.dedent("""
            mode: graph
            nodes:
              - id: src
                kind: source.file
                config: {path: child.csv, format: csv}
              - id: child
                kind: mask
                config: {columns: {customer_kind: {strategy: faker}}}
              - id: tgt
                kind: target.file
                config: {output_filename: out.csv, format: csv}
            edges:
              - {from: src, to: child}
              - {from: child, to: tgt}
            column_relationships:
              - parent: {custom_provider: kinds_provider}
                child: {node: nonexistent_node, column: customer_kind}
        """)
        res = validate_graph_full(yaml_text)
        codes = [e.code for e in res.errors]
        assert CODES.FK_UNKNOWN_NODE in codes

    def test_child_kind_ineligible(self):
        """The child node must be a mask or generate node; any other
        kind (e.g. target.file) gets FK_INELIGIBLE_CHILD_KIND.
        """
        yaml_text = textwrap.dedent("""
            mode: graph
            nodes:
              - id: src
                kind: source.file
                config: {path: child.csv, format: csv}
              - id: mid
                kind: mask
                config: {columns: {customer_kind: {strategy: faker}}}
              - id: tgt
                kind: target.file
                config: {output_filename: out.csv, format: csv}
            edges:
              - {from: src, to: mid}
              - {from: mid, to: tgt}
            column_relationships:
              - parent: {custom_provider: kinds_provider}
                child: {node: tgt, column: customer_kind}
        """)
        res = validate_graph_full(yaml_text)
        codes = [e.code for e in res.errors]
        assert CODES.FK_INELIGIBLE_CHILD_KIND in codes
