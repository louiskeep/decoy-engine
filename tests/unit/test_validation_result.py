"""R2.1: ValidationResult + validate_graph_full contract tests.

Covers the non-raising entry point introduced in R2.1. The legacy
`validate_graph` raise-style remains tested elsewhere; here we assert
that:

  - successful validation returns ok=True + a populated normalized_config
  - failed validation returns ok=False with at least one structured
    ValidationMessage carrying a stable code from CODES
  - codes survive round-trip through the per-node validator path so a
    source.file gate (e.g. SOURCE_FILE_NO_HEADER_COLUMNS) reaches the
    caller intact
"""

from __future__ import annotations

import pytest
import yaml

from decoy_engine import VALIDATION_CODES, ValidationResult, validate_graph_full


def _wrap_graph(nodes, edges=None):
    return yaml.safe_dump(
        {
            "mode": "graph",
            "schema_version": 1,
            "nodes": nodes,
            "edges": edges or [],
        }
    )


def _valid_graph():
    return _wrap_graph(
        nodes=[
            {
                "id": "src_1",
                "kind": "source.file",
                "config": {"path": "uploads/x.csv", "format": "csv"},
            },
            {
                "id": "mask_1",
                "kind": "mask",
                "config": {"columns": {"a": {"strategy": "passthrough"}}},
            },
            {
                "id": "tgt_1",
                "kind": "target.file",
                "config": {"output_filename": "out/x.csv", "format": "csv"},
            },
        ],
        edges=[
            {"from": "src_1", "to": "mask_1"},
            {"from": "mask_1", "to": "tgt_1"},
        ],
    )


class TestValidateGraphFullHappyPath:
    def test_ok_on_valid_graph(self):
        result = validate_graph_full(_valid_graph())
        assert isinstance(result, ValidationResult)
        assert result.ok is True
        assert result.errors == []
        # normalized_config is populated on success so callers can diff
        # against the original input.
        assert result.normalized_config is not None
        assert result.normalized_config["mode"] == "graph"


class TestValidateGraphFullStableCodes:
    def test_unknown_kind_yields_node_unknown_kind(self):
        yaml_text = _wrap_graph(
            nodes=[{"id": "src_1", "kind": "source.bogus", "config": {"path": "x"}}],
        )
        result = validate_graph_full(yaml_text)
        assert result.ok is False
        assert len(result.errors) == 1
        msg = result.errors[0]
        assert msg.code == VALIDATION_CODES.NODE_UNKNOWN_KIND
        assert msg.severity == "error"
        assert msg.path is not None and "kind" in msg.path

    def test_source_file_bad_delimiter_round_trips_through_node_path(self):
        # The op-level gate raises with code SOURCE_FILE_BAD_DELIMITER;
        # the graph validator re-anchors the path to nodes[N].config.* but
        # MUST preserve the code so a UI consumer can route the failure
        # without parsing the message. Note: the has_header=false gate
        # used here previously was demoted to a UI-only nudge - mid-
        # drafting users shouldn't be blocked by it.
        yaml_text = _wrap_graph(
            nodes=[
                {
                    "id": "src_1",
                    "kind": "source.file",
                    "config": {"path": "uploads/x.csv", "format": "csv", "delimiter": ""},
                }
            ],
        )
        result = validate_graph_full(yaml_text)
        assert result.ok is False
        msg = result.errors[0]
        assert msg.code == VALIDATION_CODES.SOURCE_FILE_BAD_DELIMITER
        # The path is re-anchored to the node index so the platform layer
        # can resolve node_id from the YAML by index.
        assert msg.path == "nodes[0].config.delimiter"

    def test_missing_path_yields_source_file_missing_path(self):
        yaml_text = _wrap_graph(
            nodes=[{"id": "src_1", "kind": "source.file", "config": {}}],
        )
        result = validate_graph_full(yaml_text)
        msg = result.errors[0]
        assert msg.code == VALIDATION_CODES.SOURCE_FILE_MISSING_PATH

    def test_top_level_bad_mode(self):
        yaml_text = yaml.safe_dump({"mode": "mask", "nodes": []})
        result = validate_graph_full(yaml_text)
        msg = result.errors[0]
        assert msg.code == VALIDATION_CODES.TOP_LEVEL_BAD_MODE

    def test_empty_nodes_list(self):
        yaml_text = yaml.safe_dump({"mode": "graph", "schema_version": 1, "nodes": []})
        result = validate_graph_full(yaml_text)
        msg = result.errors[0]
        assert msg.code == VALIDATION_CODES.NODES_EMPTY_LIST


class TestValidateGraphFullDoesNotRaise:
    """The R2.1 entry point's contract is: never raise on a validation
    failure. YAML-parse failures (upstream of validation) may still
    raise yaml.YAMLError; that's correct behavior."""

    def test_validation_failure_does_not_raise(self):
        yaml_text = _wrap_graph(
            nodes=[{"id": "src_1", "kind": "source.bogus", "config": {}}],
        )
        # Should NOT raise PipelineValidationError or ValidationError.
        result = validate_graph_full(yaml_text)
        assert result.ok is False

    def test_yaml_parse_failure_raises_config_error(self):
        # Genuinely broken YAML is an upstream problem, not a
        # validation outcome. The engine's _load_yaml wraps the
        # underlying YAMLError in ConfigError; that contract is
        # unchanged by R2.1.
        from decoy_engine.exceptions import ConfigError

        with pytest.raises(ConfigError):
            validate_graph_full("nodes: [unclosed")


class TestValidationResultHelpers:
    def test_add_error_populates_list(self):
        r = ValidationResult()
        r.add_error(
            code=VALIDATION_CODES.UNTAGGED,
            message="something broke",
            path="nodes[0].kind",
            node_id="src_1",
        )
        assert r.ok is False
        assert len(r.errors) == 1
        assert r.errors[0].severity == "error"
        assert r.errors[0].node_id == "src_1"

    def test_add_warning_does_not_block_ok(self):
        r = ValidationResult()
        r.add_warning(
            code="some.code",
            message="check this",
        )
        # Warnings don't make ok false.
        assert r.ok is True
        assert len(r.warnings) == 1


class TestLegacyValidateGraphStillRaises:
    """Backward compat: existing callers using the raise-style entry
    point keep working. The exception now carries a `code` attribute
    when the underlying validator tagged its failure."""

    def test_raises_with_code_attribute(self):
        from decoy_engine import validate_graph
        from decoy_engine.exceptions import PipelineValidationError

        yaml_text = _wrap_graph(
            nodes=[{"id": "src_1", "kind": "source.bogus", "config": {}}],
        )
        with pytest.raises(PipelineValidationError) as exc_info:
            validate_graph(yaml_text)
        assert exc_info.value.code == VALIDATION_CODES.NODE_UNKNOWN_KIND
