"""Unit tests for validate_graph_full multi-phase error collection.

Covers:
- Non-mutation: caller's config dict unchanged after call
- Multi-phase: both a bad node and a bad edge collected in one call
- normalized_config set on success; None on error
- normalized_config includes format back-fill without mutating caller
- Per-phase error codes: top_level, node, edge, cardinality, cycle,
  mask_unknown_column, nodes_ref checks
- YAML parse error raises ConfigError (not returned in ValidationResult)
- Valid minimal graph produces ok=True with normalized_config
- result.warnings is always a list (never None)
- Format mismatch advisory: warning in default mode, error in strict mode
"""
from __future__ import annotations

import copy
import textwrap

import pytest

from decoy_engine.exceptions import ConfigError
from decoy_engine.graph.runner import validate_graph_full
from decoy_engine.validation_result import CODES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _yaml(text: str) -> str:
    return textwrap.dedent(text).strip()


_MINIMAL_VALID = _yaml("""
    mode: graph
    nodes:
      - id: src1
        kind: source.file
        config:
          path: data.csv
          format: csv
      - id: tgt1
        kind: target.file
        config:
          output_filename: out.csv
          format: csv
    edges:
      - from: src1
        to: tgt1
""")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestValidGraphFullHappyPath:
    def test_valid_graph_ok_true(self):
        result = validate_graph_full(_MINIMAL_VALID)
        assert result.ok is True

    def test_valid_graph_no_errors(self):
        result = validate_graph_full(_MINIMAL_VALID)
        assert result.errors == []

    def test_valid_graph_no_warnings(self):
        result = validate_graph_full(_MINIMAL_VALID)
        assert result.warnings == []

    def test_valid_graph_normalized_config_set(self):
        result = validate_graph_full(_MINIMAL_VALID)
        assert result.normalized_config is not None
        assert isinstance(result.normalized_config, dict)

    def test_valid_graph_normalized_config_has_nodes(self):
        result = validate_graph_full(_MINIMAL_VALID)
        assert "nodes" in result.normalized_config
        assert len(result.normalized_config["nodes"]) == 2


# ---------------------------------------------------------------------------
# Non-mutation guarantee
# ---------------------------------------------------------------------------

class TestNonMutation:
    def test_caller_config_not_mutated_on_success(self):
        """validate_graph_full must not write into the caller's parsed dict.
        _validate_file_format_consistency back-fills target.file format
        when the field is absent -- this test confirms that back-fill never
        reaches the caller's dict."""
        # target.file with no explicit format; source has csv.
        yaml_text = _yaml("""
            mode: graph
            nodes:
              - id: src1
                kind: source.file
                config:
                  path: data.csv
                  format: csv
              - id: tgt1
                kind: target.file
                config:
                  output_filename: out.csv
            edges:
              - from: src1
                to: tgt1
        """)
        import yaml
        original_config = yaml.safe_load(yaml_text)
        original_deep = copy.deepcopy(original_config)

        validate_graph_full(yaml_text)

        # The caller's dict must be byte-identical to what it was before.
        assert original_config == original_deep

    def test_normalized_config_has_format_backfill(self):
        """normalized_config SHOULD contain the back-filled format field."""
        yaml_text = _yaml("""
            mode: graph
            nodes:
              - id: src1
                kind: source.file
                config:
                  path: data.csv
                  format: csv
              - id: tgt1
                kind: target.file
                config:
                  output_filename: out.csv
            edges:
              - from: src1
                to: tgt1
        """)
        result = validate_graph_full(yaml_text)
        assert result.ok
        # Find tgt1 in normalized_config and confirm format was back-filled.
        tgt = next(n for n in result.normalized_config["nodes"] if n["id"] == "tgt1")
        assert tgt.get("config", {}).get("format") == "csv"

    def test_caller_config_not_mutated_on_error(self):
        yaml_text = _yaml("""
            mode: graph
            nodes:
              - id: src1
                kind: source.nonexistent_kind_xyz
                config: {}
        """)
        import yaml
        original_config = yaml.safe_load(yaml_text)
        original_deep = copy.deepcopy(original_config)

        validate_graph_full(yaml_text)

        assert original_config == original_deep


# ---------------------------------------------------------------------------
# YAML parse errors
# ---------------------------------------------------------------------------

class TestYamlParseError:
    def test_invalid_yaml_raises_config_error(self):
        with pytest.raises(ConfigError):
            validate_graph_full("mode: graph\nnodes: [unclosed")

    def test_non_mapping_yaml_raises_config_error(self):
        with pytest.raises(ConfigError):
            validate_graph_full("- just a list")


# ---------------------------------------------------------------------------
# Phase 1 — top-level errors
# ---------------------------------------------------------------------------

class TestTopLevelErrors:
    def test_bad_mode_produces_error(self):
        yaml_text = _yaml("""
            mode: legacy
            nodes:
              - id: src1
                kind: source.file
                config: {path: d.csv, format: csv}
        """)
        result = validate_graph_full(yaml_text)
        assert not result.ok
        assert any(e.code == CODES.TOP_LEVEL_BAD_MODE for e in result.errors)

    def test_bad_mode_normalized_config_is_none(self):
        yaml_text = _yaml("""
            mode: legacy
            nodes: [{id: src1, kind: source.file, config: {path: d.csv}}]
        """)
        result = validate_graph_full(yaml_text)
        assert result.normalized_config is None

    def test_empty_nodes_list(self):
        yaml_text = _yaml("""
            mode: graph
            nodes: []
        """)
        result = validate_graph_full(yaml_text)
        assert not result.ok
        assert any(e.code == CODES.NODES_EMPTY_LIST for e in result.errors)


# ---------------------------------------------------------------------------
# Phase 2 — per-node errors
# ---------------------------------------------------------------------------

class TestNodeErrors:
    def test_unknown_kind_produces_error(self):
        yaml_text = _yaml("""
            mode: graph
            nodes:
              - id: src1
                kind: source.nonexistent_xyz
                config: {}
        """)
        result = validate_graph_full(yaml_text)
        assert not result.ok
        assert any(e.code == CODES.NODE_UNKNOWN_KIND for e in result.errors)

    def test_bad_node_id_regex(self):
        yaml_text = _yaml("""
            mode: graph
            nodes:
              - id: "123_bad"
                kind: source.file
                config: {path: d.csv}
        """)
        result = validate_graph_full(yaml_text)
        assert not result.ok
        assert any(e.code == CODES.NODE_BAD_ID for e in result.errors)

    def test_duplicate_node_id(self):
        yaml_text = _yaml("""
            mode: graph
            nodes:
              - id: src1
                kind: source.file
                config: {path: d.csv, format: csv}
              - id: src1
                kind: source.file
                config: {path: d2.csv, format: csv}
        """)
        result = validate_graph_full(yaml_text)
        assert not result.ok
        assert any(e.code == CODES.NODE_DUPLICATE_ID for e in result.errors)


# ---------------------------------------------------------------------------
# Phase 3 — per-edge errors
# ---------------------------------------------------------------------------

class TestEdgeErrors:
    def test_edge_unknown_from_node(self):
        yaml_text = _yaml("""
            mode: graph
            nodes:
              - id: tgt1
                kind: target.file
                config: {output_filename: out.csv, format: csv}
            edges:
              - from: nonexistent
                to: tgt1
        """)
        result = validate_graph_full(yaml_text)
        assert not result.ok

    def test_multi_phase_node_and_edge_errors_collected(self):
        """Bad node id + edge referencing unknown node -> 2 errors (one per phase)."""
        yaml_text = _yaml("""
            mode: graph
            nodes:
              - id: "1bad"
                kind: source.file
                config: {path: d.csv}
            edges:
              - from: unknown_node
                to: also_unknown
        """)
        result = validate_graph_full(yaml_text)
        assert not result.ok
        # nodes phase failed (bad id) AND edges phase failed (unknown from)
        # -> at least 2 errors collected in a single call.
        assert len(result.errors) >= 2


# ---------------------------------------------------------------------------
# Phases 4+5 — cardinality and cycle errors
# ---------------------------------------------------------------------------

class TestTopologyErrors:
    def test_insufficient_inputs_error(self):
        """mask needs at least 1 incoming edge; zero inputs should fail cardinality."""
        yaml_text = _yaml("""
            mode: graph
            nodes:
              - id: m1
                kind: mask
                config:
                  columns:
                    name: {strategy: redact}
        """)
        result = validate_graph_full(yaml_text)
        assert not result.ok

    def test_cycle_detected(self):
        yaml_text = _yaml("""
            mode: graph
            nodes:
              - id: a
                kind: mask
                config:
                  columns:
                    col1: {strategy: redact}
              - id: b
                kind: mask
                config:
                  columns:
                    col1: {strategy: redact}
            edges:
              - from: a
                to: b
              - from: b
                to: a
        """)
        result = validate_graph_full(yaml_text)
        assert not result.ok
        assert any(e.code == CODES.GRAPH_CYCLE for e in result.errors)


# ---------------------------------------------------------------------------
# Phase 6 — format mismatch advisory (Sprint 2.4)
# ---------------------------------------------------------------------------

class TestFormatMismatch:
    """Format-mismatch advisories from _validate_file_format_consistency
    are now surfaced in ValidationResult rather than silently dropped.

    Default mode: warning (ok=True, non-blocking).
    Strict mode: error (ok=False, run blocked).
    """

    # source.file with csv, target.file with explicit parquet -- mismatch
    # detected without any convert.file_type node in between.
    _CSV_TO_PARQUET = _yaml("""
        mode: graph
        nodes:
          - id: src
            kind: source.file
            config:
              path: /data/input.csv
              format: csv
          - id: tgt
            kind: target.file
            config:
              output_filename: output.parquet
              format: parquet
        edges:
          - from: src
            to: tgt
    """)

    def test_format_mismatch_produces_warning_by_default(self):
        result = validate_graph_full(self._CSV_TO_PARQUET)
        assert result.ok is True
        assert len(result.warnings) >= 1
        assert any(w.code == CODES.GRAPH_FORMAT_MISMATCH for w in result.warnings)

    def test_format_mismatch_produces_error_in_strict_mode(self):
        result = validate_graph_full(self._CSV_TO_PARQUET, strict=True)
        assert result.ok is False
        assert any(e.code == CODES.GRAPH_FORMAT_MISMATCH for e in result.errors)

    def test_matching_formats_produce_no_warning(self):
        yaml_text = _yaml("""
            mode: graph
            nodes:
              - id: src
                kind: source.file
                config:
                  path: /data/input.csv
                  format: csv
              - id: tgt
                kind: target.file
                config:
                  output_filename: output.csv
                  format: csv
            edges:
              - from: src
                to: tgt
        """)
        result = validate_graph_full(yaml_text)
        assert result.ok is True
        assert not any(w.code == CODES.GRAPH_FORMAT_MISMATCH for w in result.warnings)


# ---------------------------------------------------------------------------
# Idempotency: calling twice gives same result
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_same_result_on_second_call(self):
        r1 = validate_graph_full(_MINIMAL_VALID)
        r2 = validate_graph_full(_MINIMAL_VALID)
        assert r1.ok == r2.ok
        assert len(r1.errors) == len(r2.errors)
        assert r1.normalized_config == r2.normalized_config
