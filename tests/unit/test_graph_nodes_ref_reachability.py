"""R2.3: cross-node validation - ${nodes.<id>.<key>} reachability.

At run time the engine resolves ``${nodes.<id>.<key>}`` against
in-memory exports captured from already-completed nodes. If the
referenced id doesn't exist (typo) or the referenced node isn't an
upstream of the referrer (will never be set when this node runs),
the runner consumes a bogus value and finishes successfully with
wrong data. This validator turns both into pre-run errors.
"""
from __future__ import annotations

import yaml

from decoy_engine import validate_graph_full, VALIDATION_CODES


def _wrap_graph(nodes, edges):
    return yaml.safe_dump({
        "mode": "graph",
        "schema_version": 1,
        "nodes": nodes,
        "edges": edges,
    })


def _three_node_pipeline(target_filename: str):
    """src_1 -> mask_1 -> tgt_1, target's output_filename templated."""
    return _wrap_graph(
        nodes=[
            {"id": "src_1", "kind": "source.file", "config": {
                "path": "uploads/x.csv", "format": "csv",
                "has_header": True,
            }},
            {"id": "mask_1", "kind": "mask", "config": {
                "columns": {"id": {"strategy": "passthrough"}},
            }},
            {"id": "tgt_1", "kind": "target.file", "config": {
                "output_filename": target_filename, "format": "csv",
            }},
        ],
        edges=[
            {"from": "src_1", "to": "mask_1"},
            {"from": "mask_1", "to": "tgt_1"},
        ],
    )


class TestNodesRefReachability:
    def test_valid_upstream_reference_passes(self):
        # tgt_1 references src_1 (transitive upstream via mask_1). Fine.
        yaml_text = _three_node_pipeline(
            "out_${nodes.src_1.row_count}.csv",
        )
        result = validate_graph_full(yaml_text)
        assert result.ok, [m.message for m in result.errors]

    def test_unknown_id_blocks(self):
        yaml_text = _three_node_pipeline(
            "out_${nodes.ghost.row_count}.csv",
        )
        result = validate_graph_full(yaml_text)
        assert not result.ok
        msg = result.errors[0]
        assert msg.code == VALIDATION_CODES.NODES_REF_UNKNOWN_ID
        assert "ghost" in msg.message
        assert msg.path == "nodes.tgt_1.config"

    def test_existing_id_but_not_upstream_blocks(self):
        # tgt_1 references mask_1 -> upstream, fine.
        # But mask_1 references tgt_1 -> downstream, NOT upstream.
        # Put the bad ref in mask_1's config so the referrer differs
        # from the target id and we exercise the not_upstream branch.
        yaml_text = _wrap_graph(
            nodes=[
                {"id": "src_1", "kind": "source.file", "config": {
                    "path": "uploads/x.csv", "format": "csv",
                    "has_header": True,
                }},
                {"id": "mask_1", "kind": "mask", "config": {
                    # The column name is fine on the source schema (id
                    # passthrough). The bad token sits in the strategy
                    # params, which the engine treats as opaque config
                    # at validate time.
                    "columns": {
                        "id": {
                            "strategy": "passthrough",
                            "note": "downstream id ${nodes.tgt_1.path}",
                        },
                    },
                }},
                {"id": "tgt_1", "kind": "target.file", "config": {
                    "output_filename": "out.csv", "format": "csv",
                }},
            ],
            edges=[
                {"from": "src_1", "to": "mask_1"},
                {"from": "mask_1", "to": "tgt_1"},
            ],
        )
        result = validate_graph_full(yaml_text)
        assert not result.ok
        msg = result.errors[0]
        assert msg.code == VALIDATION_CODES.NODES_REF_NOT_UPSTREAM
        assert "tgt_1" in msg.message
        assert msg.path == "nodes.mask_1.config"

    def test_source_node_referencing_anything_is_not_upstream(self):
        # A source node has no incoming edges, so any ${nodes...}
        # reference inside its config is automatically not upstream.
        # The ref lives in a free-form note field so the source.file
        # validator doesn't reject it on its own grounds.
        yaml_text = _wrap_graph(
            nodes=[
                {"id": "src_1", "kind": "source.file", "config": {
                    "path": "uploads/x.csv", "format": "csv",
                    "has_header": True,
                    "note": "see ${nodes.mask_1.row_count}",
                }},
                {"id": "mask_1", "kind": "mask", "config": {
                    "columns": {"id": {"strategy": "passthrough"}},
                }},
                {"id": "tgt_1", "kind": "target.file", "config": {
                    "output_filename": "out.csv", "format": "csv",
                }},
            ],
            edges=[
                {"from": "src_1", "to": "mask_1"},
                {"from": "mask_1", "to": "tgt_1"},
            ],
        )
        result = validate_graph_full(yaml_text)
        assert not result.ok
        msg = result.errors[0]
        assert msg.code == VALIDATION_CODES.NODES_REF_NOT_UPSTREAM
