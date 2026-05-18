"""Sprint 2.1: validate_graph_full returns a normalized copy of the config.

The validator's _validate_file_format_consistency back-fills target.file
nodes that omit 'format' in-place. With Sprint 2.1, validate_graph_full
passes a deep copy to the validator so those normalizations appear only
in result.normalized_config without affecting any live config reference.

Two test groups:
  - TestValidateGraphFullNormalizedConfig: public API contract
  - TestGraphConfigValidatorDirectMutation: verifies that direct
    GraphConfigValidator.validate(cfg) is now pure — Sprint 2.2 removed
    the in-place back-fill. The normalizations live in validate_graph_full.
"""
import logging

import yaml

from decoy_engine.graph.runner import validate_graph_full
from decoy_engine.internal.validator import GraphConfigValidator


# --- YAML fixtures --------------------------------------------------------

_PARQUET_PIPELINE = """\
mode: graph
nodes:
  - id: src
    kind: source.file
    config:
      path: input.parquet
  - id: tgt
    kind: target.file
    config:
      output_filename: output.parquet
edges:
  - from: src
    to: tgt
"""

_CSV_PIPELINE = """\
mode: graph
nodes:
  - id: src
    kind: source.file
    config:
      path: input.csv
  - id: tgt
    kind: target.file
    config:
      output_filename: output.csv
edges:
  - from: src
    to: tgt
"""

_BAD_PIPELINE = """\
mode: graph
nodes:
  - id: src
    kind: source.file
    config:
      path: input.csv
  - id: tgt
    kind: unknown_kind
    config: {}
edges:
  - from: src
    to: tgt
"""


# --- validate_graph_full: public API contract ------------------------------

class TestValidateGraphFullNormalizedConfig:
    def test_normalized_config_populated_on_success(self):
        result = validate_graph_full(_PARQUET_PIPELINE)
        assert result.ok
        assert result.normalized_config is not None

    def test_two_calls_return_independent_normalized_configs(self):
        r1 = validate_graph_full(_PARQUET_PIPELINE)
        r2 = validate_graph_full(_PARQUET_PIPELINE)
        assert r1.ok and r2.ok
        assert r1.normalized_config is not r2.normalized_config

    def test_normalized_config_contains_back_filled_format(self):
        """The format back-fill appears in normalized_config, not in the raw YAML."""
        raw_config = yaml.safe_load(_PARQUET_PIPELINE)
        tgt_raw = next(n for n in raw_config["nodes"] if n["id"] == "tgt")
        assert "format" not in (tgt_raw.get("config") or {})

        result = validate_graph_full(_PARQUET_PIPELINE)
        assert result.ok
        tgt_norm = next(
            n for n in result.normalized_config["nodes"] if n["id"] == "tgt"
        )
        assert tgt_norm["config"]["format"] == "parquet"

    def test_csv_pipeline_normalized_config_has_csv_format(self):
        result = validate_graph_full(_CSV_PIPELINE)
        assert result.ok
        tgt_norm = next(
            n for n in result.normalized_config["nodes"] if n["id"] == "tgt"
        )
        assert tgt_norm["config"]["format"] == "csv"

    def test_normalized_config_is_none_on_error(self):
        result = validate_graph_full(_BAD_PIPELINE)
        assert not result.ok
        assert result.normalized_config is None

    def test_error_carries_code_and_path(self):
        result = validate_graph_full(_BAD_PIPELINE)
        assert len(result.errors) == 1
        assert result.errors[0].code is not None
        assert result.errors[0].path is not None


# --- Direct validator: back-fill mutation is a known behavior -------------

class TestGraphConfigValidatorDirectMutation:
    """Sprint 2.2: GraphConfigValidator.validate() is now pure — it no longer
    back-fills target.file config.format in-place. Normalizations are applied
    explicitly by validate_graph_full via _backfill_target_file_formats so only
    result.normalized_config carries them.
    """

    def test_does_not_back_fill_format_on_parquet(self):
        tgt_cfg = {"output_filename": "out.parquet"}
        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.file", "config": {"path": "in.parquet"}},
                {"id": "tgt", "kind": "target.file", "config": tgt_cfg},
            ],
            "edges": [{"from": "src", "to": "tgt"}],
        }
        GraphConfigValidator(logging.getLogger("test")).validate(cfg)
        assert "format" not in tgt_cfg

    def test_does_not_back_fill_format_on_csv(self):
        tgt_cfg = {"output_filename": "out.csv"}
        cfg = {
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.file", "config": {"path": "in.csv"}},
                {"id": "tgt", "kind": "target.file", "config": tgt_cfg},
            ],
            "edges": [{"from": "src", "to": "tgt"}],
        }
        GraphConfigValidator(logging.getLogger("test")).validate(cfg)
        assert "format" not in tgt_cfg
