"""Correction gate test: validate_graph + validate_graph_full FK parity.

Dennis review of `v2/0b-validator-split` found that the raise-style
`validate_graph(...)` skipped the FK / `column_relationships` stage
that `validate_graph_full(...)` runs. A config with a missing FK
parent, invalid child, bad column, or strict-mode relationship error
would pass the raise-style path even though the non-raising full
path rejected it.

These tests pin the parity: every FK fixture that surfaces an error
through `validate_graph_full` must now raise through
`validate_graph`. The fixtures reuse the same minimal two-table
graph the existing `test_validate_fk_relationships.py` suite uses
so the two test files stay in sync.
"""

from __future__ import annotations

import textwrap

import pytest

from decoy_engine.errors import PipelineValidationError
from decoy_engine.graph.runner import validate_graph, validate_graph_full
from decoy_engine.validation_result import CODES


def _two_table_with_fk(
    *,
    parent_node: str = "mask_1",
    parent_column: str = "id",
    child_node: str = "mask_2",
    child_column: str = "customer_id",
) -> str:
    """Minimal two-table graph with a declared FK between
    `parent_node.parent_column` and `child_node.child_column`.
    Override any field to construct a failing fixture."""
    return textwrap.dedent(f"""
        mode: graph
        nodes:
          - id: src_1
            kind: source.file
            config: {{path: parent.csv, format: csv}}
          - id: mask_1
            kind: mask
            config: {{columns: {{id: {{strategy: hash}}}}}}
          - id: tgt_1
            kind: target.file
            config: {{output_filename: p.csv, format: csv}}
          - id: src_2
            kind: source.file
            config: {{path: child.csv, format: csv}}
          - id: mask_2
            kind: mask
            config: {{columns: {{customer_id: {{strategy: hash}}}}}}
          - id: tgt_2
            kind: target.file
            config: {{output_filename: c.csv, format: csv}}
        edges:
          - {{from: src_1, to: mask_1}}
          - {{from: mask_1, to: tgt_1}}
          - {{from: src_2, to: mask_2}}
          - {{from: mask_2, to: tgt_2}}
        column_relationships:
          - parent: {{node: {parent_node}, column: {parent_column}}}
            child: {{node: {child_node}, column: {child_column}}}
    """)


# ── parity: errors in full must raise in raise-style ────────────────────────


def test_parent_node_missing_raises():
    yaml = _two_table_with_fk(parent_node="nonexistent_parent")
    # Sanity: full path already surfaces this.
    full = validate_graph_full(yaml)
    assert CODES.FK_UNKNOWN_NODE in [e.code for e in full.errors]
    # Parity: raise-style must also reject.
    with pytest.raises(PipelineValidationError) as exc_info:
        validate_graph(yaml)
    assert exc_info.value.code == CODES.FK_UNKNOWN_NODE


def test_child_node_missing_raises():
    yaml = _two_table_with_fk(child_node="nonexistent_child")
    full = validate_graph_full(yaml)
    assert CODES.FK_UNKNOWN_NODE in [e.code for e in full.errors]
    with pytest.raises(PipelineValidationError) as exc_info:
        validate_graph(yaml)
    assert exc_info.value.code == CODES.FK_UNKNOWN_NODE


def test_child_column_unknown_raises():
    yaml = _two_table_with_fk(child_column="ghost")
    full = validate_graph_full(yaml)
    assert CODES.FK_UNKNOWN_COLUMN in [e.code for e in full.errors]
    with pytest.raises(PipelineValidationError) as exc_info:
        validate_graph(yaml)
    assert exc_info.value.code == CODES.FK_UNKNOWN_COLUMN


def test_parent_column_unknown_raises():
    yaml = _two_table_with_fk(parent_column="ghost_parent_column")
    full = validate_graph_full(yaml)
    assert CODES.FK_UNKNOWN_COLUMN in [e.code for e in full.errors]
    with pytest.raises(PipelineValidationError) as exc_info:
        validate_graph(yaml)
    assert exc_info.value.code == CODES.FK_UNKNOWN_COLUMN


def test_self_cycle_raises():
    # Parent and child point at the same node + column: a self-cycle
    # within one node's column relationships. validate_graph_full now
    # emits FK_SELF_CYCLE (the legacy FK_SELF_REFERENCE is a warning
    # for non-same-column self pairs); the raise path must match.
    yaml = _two_table_with_fk(
        parent_node="mask_1",
        parent_column="id",
        child_node="mask_1",
        child_column="id",
    )
    full = validate_graph_full(yaml)
    assert CODES.FK_SELF_CYCLE in [e.code for e in full.errors]
    with pytest.raises(PipelineValidationError) as exc_info:
        validate_graph(yaml)
    assert exc_info.value.code == CODES.FK_SELF_CYCLE


# ── parity: a valid graph passes both ───────────────────────────────────────


def test_valid_two_table_passes_both_paths():
    yaml = _two_table_with_fk()
    full = validate_graph_full(yaml)
    assert full.errors == []
    # Raise-style does not raise.
    validate_graph(yaml)


def test_raise_carries_path_and_code_from_first_fk_error():
    yaml = _two_table_with_fk(child_node="nonexistent_child")
    with pytest.raises(PipelineValidationError) as exc_info:
        validate_graph(yaml)
    # The error object carries the structured fields from the FK
    # validator so a platform caller can route the failure without
    # string-parsing.
    assert exc_info.value.code == CODES.FK_UNKNOWN_NODE
    assert exc_info.value.path is not None
