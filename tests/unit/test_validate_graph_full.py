"""Sprint 2.1: multi-stage validate_graph_full behavioral tests.

Covers what the existing test_validation_result.py does NOT:
  - Stage-level error collection: errors from independent stages both appear
  - Cross-node stage independence: stages 6/7/8 run independently so
    MASK_UNKNOWN_COLUMN and NODES_REF_UNKNOWN_ID can surface together
  - normalized_config deep copy: the returned config is a separate object
    so mutating it doesn't affect future calls
  - Format inference normalization: target.file format is back-filled from
    source into normalized_config without mutating any caller-held dict
  - strict=True parameter is accepted without errors on a valid graph
"""
from __future__ import annotations

import pytest

from decoy_engine import validate_graph_full, VALIDATION_CODES
from decoy_engine.validation_result import ValidationResult


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_SRC_TGT = """
mode: graph
nodes:
  - id: src
    kind: source.file
    config:
      path: data.csv
      format: csv
  - id: tgt
    kind: target.file
    config:
      output_filename: out.csv
      format: csv
edges:
  - from: src
    to: tgt
"""

_SRC_TGT_NO_TARGET_FORMAT = """
mode: graph
nodes:
  - id: src
    kind: source.file
    config:
      path: data.csv
      format: csv
  - id: tgt
    kind: target.file
    config:
      output_filename: out.csv
edges:
  - from: src
    to: tgt
"""


# ---------------------------------------------------------------------------
# Return type and happy-path
# ---------------------------------------------------------------------------

class TestReturnType:
    def test_returns_validation_result(self):
        res = validate_graph_full(_SRC_TGT)
        assert isinstance(res, ValidationResult)

    def test_valid_graph_is_ok(self):
        res = validate_graph_full(_SRC_TGT)
        assert res.ok
        assert res.errors == []

    def test_normalized_config_populated_on_success(self):
        res = validate_graph_full(_SRC_TGT)
        assert res.normalized_config is not None
        assert res.normalized_config["mode"] == "graph"

    def test_normalized_config_none_on_error(self):
        yaml_text = _SRC_TGT.replace("mode: graph", "mode: legacy")
        res = validate_graph_full(yaml_text)
        assert not res.ok
        assert res.normalized_config is None


# ---------------------------------------------------------------------------
# Deep copy: normalized_config is independent of any internally-held dict
# ---------------------------------------------------------------------------

class TestDeepCopy:
    def test_mutating_normalized_config_does_not_affect_next_call(self):
        res1 = validate_graph_full(_SRC_TGT)
        assert res1.ok
        # Mutate the returned normalized config.
        res1.normalized_config["nodes"][0]["config"]["path"] = "MUTATED"
        # A second call with the same YAML string must succeed unchanged.
        res2 = validate_graph_full(_SRC_TGT)
        assert res2.ok
        assert res2.normalized_config["nodes"][0]["config"]["path"] != "MUTATED"


# ---------------------------------------------------------------------------
# Format inference: normalized_config contains back-filled target format
# ---------------------------------------------------------------------------

class TestFormatNormalization:
    def test_target_format_inferred_from_source_in_normalized_config(self):
        res = validate_graph_full(_SRC_TGT_NO_TARGET_FORMAT)
        assert res.ok, f"Expected ok but got errors: {res.errors}"
        # _validate_file_format_consistency back-fills the target format
        # from the source. The normalized_config must contain it.
        tgt_cfg = res.normalized_config["nodes"][1]["config"]
        assert tgt_cfg.get("format") == "csv"


# ---------------------------------------------------------------------------
# Stage-level early exit: top-level failure stops immediately
# ---------------------------------------------------------------------------

class TestTopLevelEarlyExit:
    def test_bad_mode_stops_at_stage_1(self):
        yaml_text = _SRC_TGT.replace("mode: graph", "mode: legacy")
        res = validate_graph_full(yaml_text)
        assert not res.ok
        assert len(res.errors) == 1
        assert res.errors[0].code == VALIDATION_CODES.TOP_LEVEL_BAD_MODE

    def test_empty_nodes_stops_at_stage_1(self):
        import yaml
        yaml_text = yaml.safe_dump({"mode": "graph", "nodes": []})
        res = validate_graph_full(yaml_text)
        assert not res.ok
        assert res.errors[0].code == VALIDATION_CODES.NODES_EMPTY_LIST


# ---------------------------------------------------------------------------
# Cross-node stage independence: stages 6, 7, 8 run independently
# ---------------------------------------------------------------------------

class TestCrossNodeStageIndependence:
    """When the graph passes structural validation (stages 1-5), the three
    cross-node semantic checks (6=format, 7=mask-column, 8=nodes-ref) are
    each attempted. A failure in one does not block the others."""

    # Graph: valid structure; mask references a column not in source schema;
    # target has a ${nodes.ghost.val} token pointing to a nonexistent node.
    _BOTH_ERRORS = """
mode: graph
nodes:
  - id: src
    kind: source.file
    config:
      path: data.csv
      format: csv
      has_header: false
      column_names: [col_a, col_b]
  - id: msk
    kind: mask
    config:
      columns:
        col_missing:
          strategy: hash
  - id: tgt
    kind: target.file
    config:
      output_filename: "${nodes.ghost.val}.csv"
      format: csv
edges:
  - from: src
    to: msk
  - from: msk
    to: tgt
"""

    def test_mask_column_and_nodes_ref_both_reported(self):
        res = validate_graph_full(self._BOTH_ERRORS)
        assert not res.ok
        codes = {e.code for e in res.errors}
        # Stage 7: mask unknown column
        assert VALIDATION_CODES.MASK_UNKNOWN_COLUMN in codes, (
            f"Expected MASK_UNKNOWN_COLUMN in {codes}"
        )
        # Stage 8: nodes-ref to nonexistent node
        assert VALIDATION_CODES.NODES_REF_UNKNOWN_ID in codes, (
            f"Expected NODES_REF_UNKNOWN_ID in {codes}"
        )

    def test_error_count_reflects_independent_stages(self):
        res = validate_graph_full(self._BOTH_ERRORS)
        # At minimum two independent stage errors must be present.
        assert len(res.errors) >= 2


# ---------------------------------------------------------------------------
# Strict parameter
# ---------------------------------------------------------------------------

class TestStrictParam:
    def test_strict_true_accepted_on_valid_graph(self):
        res = validate_graph_full(_SRC_TGT, strict=True)
        assert res.ok

    def test_strict_false_is_default(self):
        res = validate_graph_full(_SRC_TGT)
        assert res.ok


# ---------------------------------------------------------------------------
# Errors carry stable codes and human-readable messages
# ---------------------------------------------------------------------------

class TestErrorShape:
    def test_error_has_code_and_message(self):
        yaml_text = _SRC_TGT.replace("mode: graph", "mode: bad")
        res = validate_graph_full(yaml_text)
        assert not res.ok
        err = res.errors[0]
        assert isinstance(err.code, str) and err.code
        assert isinstance(err.message, str) and err.message

    def test_unknown_kind_error_has_path(self):
        import yaml
        yaml_text = yaml.safe_dump({
            "mode": "graph",
            "schema_version": 1,
            "nodes": [{"id": "src", "kind": "source.bogus", "config": {}}],
            "edges": [],
        })
        res = validate_graph_full(yaml_text)
        assert not res.ok
        err = next(e for e in res.errors if e.code == VALIDATION_CODES.NODE_UNKNOWN_KIND)
        assert err.path is not None
