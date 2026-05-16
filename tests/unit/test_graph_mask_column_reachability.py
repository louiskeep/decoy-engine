"""R2.3: cross-node validation - mask column reachability.

Replaces the demoted has_header=false-without-column_names gate with
a smarter check: instead of blocking the source config in isolation,
block only when a downstream mask actually references column names
the source cannot produce. Permissive when the source's schema isn't
statically derivable (CSV with has_header=true, parquet without
preview, etc.) so we don't false-positive on headered pipelines.
"""
from __future__ import annotations

import yaml
import pytest

from decoy_engine import validate_graph_full, VALIDATION_CODES


def _wrap_graph(nodes, edges):
    return yaml.safe_dump({
        "mode": "graph",
        "schema_version": 1,
        "nodes": nodes,
        "edges": edges,
    })


def _mask_pipeline(source_cfg, mask_cols):
    """Helper: build a src -> mask -> tgt pipeline with the given
    source.file config and mask columns dict."""
    return _wrap_graph(
        nodes=[
            {"id": "src_1", "kind": "source.file", "config": source_cfg},
            {"id": "mask_1", "kind": "mask",
             "config": {"columns": mask_cols}},
            {"id": "tgt_1", "kind": "target.file",
             "config": {"output_filename": "out.csv", "format": "csv"}},
        ],
        edges=[
            {"from": "src_1", "to": "mask_1"},
            {"from": "mask_1", "to": "tgt_1"},
        ],
    )


class TestMaskUnknownColumn:
    def test_source_with_column_names_accepts_matching_mask(self):
        yaml_text = _mask_pipeline(
            source_cfg={
                "path": "uploads/x.csv", "format": "csv",
                "has_header": False, "column_names": ["id", "name", "amount"],
            },
            mask_cols={"id": {"strategy": "passthrough"}, "name": {"strategy": "hash"}},
        )
        result = validate_graph_full(yaml_text)
        assert result.ok, [m.message for m in result.errors]

    def test_source_with_column_names_blocks_unknown_mask_column(self):
        yaml_text = _mask_pipeline(
            source_cfg={
                "path": "uploads/x.csv", "format": "csv",
                "has_header": False, "column_names": ["id", "name", "amount"],
            },
            mask_cols={"emial": {"strategy": "hash"}},  # typo
        )
        result = validate_graph_full(yaml_text)
        assert not result.ok
        msg = result.errors[0]
        assert msg.code == VALIDATION_CODES.MASK_UNKNOWN_COLUMN
        assert "emial" in msg.message
        # The "available columns" hint surfaces the real list so the
        # user sees what they could have meant.
        assert "id" in msg.message and "name" in msg.message

    def test_has_header_false_no_column_names_blocks_real_name_mask(self):
        # This is the bug the demoted gate used to catch. R2.3 catches
        # it at the right node (mask, not source) with the right
        # rationale (silent no-op).
        yaml_text = _mask_pipeline(
            source_cfg={
                "path": "uploads/x.csv", "format": "csv",
                "has_header": False,
            },
            mask_cols={"id": {"strategy": "passthrough"}},
        )
        result = validate_graph_full(yaml_text)
        assert not result.ok
        msg = result.errors[0]
        assert msg.code == VALIDATION_CODES.MASK_UNKNOWN_COLUMN
        assert "silently no-op" in msg.message

    def test_has_header_false_no_column_names_accepts_column_n_names(self):
        # If the user really does want to mask the auto-named columns,
        # the column<int> names should pass through.
        yaml_text = _mask_pipeline(
            source_cfg={
                "path": "uploads/x.csv", "format": "csv",
                "has_header": False,
            },
            mask_cols={"column0": {"strategy": "hash"}, "column1": {"strategy": "passthrough"}},
        )
        result = validate_graph_full(yaml_text)
        assert result.ok, [m.message for m in result.errors]

    def test_has_header_true_skips_check_when_columns_unknown(self):
        # Permissive when we can't tell - CSV with has_header=true
        # reads names from the file at run time. We could ask the
        # platform to fill in a known schema (R2.4 preflight) but
        # engine-only validation has to skip.
        yaml_text = _mask_pipeline(
            source_cfg={
                "path": "uploads/x.csv", "format": "csv",
                "has_header": True,
            },
            mask_cols={"id": {"strategy": "passthrough"}},
        )
        result = validate_graph_full(yaml_text)
        assert result.ok, [m.message for m in result.errors]

    def test_fixed_width_source_accepts_fw_column_names(self):
        yaml_text = _mask_pipeline(
            source_cfg={
                "path": "uploads/x.dat", "format": "fixed_width",
                "fw_columns": [
                    {"name": "id", "start": 1, "length": 4},
                    {"name": "name", "start": 5, "length": 20},
                ],
            },
            mask_cols={"id": {"strategy": "passthrough"}, "name": {"strategy": "faker"}},
        )
        result = validate_graph_full(yaml_text)
        assert result.ok, [m.message for m in result.errors]

    def test_fixed_width_source_blocks_unknown_mask_column(self):
        yaml_text = _mask_pipeline(
            source_cfg={
                "path": "uploads/x.dat", "format": "fixed_width",
                "fw_columns": [
                    {"name": "id", "start": 1, "length": 4},
                    {"name": "name", "start": 5, "length": 20},
                ],
            },
            mask_cols={"missing_col": {"strategy": "passthrough"}},
        )
        result = validate_graph_full(yaml_text)
        assert not result.ok
        assert result.errors[0].code == VALIDATION_CODES.MASK_UNKNOWN_COLUMN

    def test_parquet_source_skips_check(self):
        # Parquet schemas live in the file; validate-time check has
        # to skip until R2.4 preflight reads the file.
        yaml_text = _mask_pipeline(
            source_cfg={
                "path": "uploads/x.parquet", "format": "parquet",
            },
            mask_cols={"id": {"strategy": "passthrough"}},
        )
        result = validate_graph_full(yaml_text)
        assert result.ok, [m.message for m in result.errors]


class TestPathRouting:
    """The new code routes through nodes.{mask_id}.config.columns.{col_name}
    so the platform's resolver can identify the mask node + the
    specific bad column key for inspector-level highlighting."""

    def test_path_anchors_on_mask_node_and_column(self):
        yaml_text = _mask_pipeline(
            source_cfg={
                "path": "uploads/x.csv", "format": "csv",
                "has_header": False, "column_names": ["id"],
            },
            mask_cols={"unknown_col": {"strategy": "hash"}},
        )
        result = validate_graph_full(yaml_text)
        msg = result.errors[0]
        # The path uses the mask id (not nodes[i]) because the check is
        # cross-node. Platform resolver still needs to handle this form.
        assert msg.path == "nodes.mask_1.config.columns.unknown_col"
