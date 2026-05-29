"""Strict-validation tests for PipelineConfig per Dennis's pipeline-config handoff §4.3.

Five categories:
- Negative tests (4+): a typo in cardinality_mode, missing orphan_policy,
  unknown source type, unknown top-level key.
- Round-trip (§4.5): model_validate(parsed_yaml).model_dump() round-trips
  cleanly through a second validate call.
- Plus a positive-path smoke test that the advisory sketch validates.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from decoy_engine.config import PipelineConfig, PipelineConfigError, override_sources

ADVISORY_SKETCH_YAML = """
version: 1
global_settings:
  seed: 42
  post_validation: false
sources:
  customers:
    type: file
    format: csv
    path: ./data/customers.csv
  orders:
    type: file
    format: csv
    path: ./data/orders.csv
tables:
  - name: customers
    columns:
      - name: customer_id
        strategy: preserve_format_id
        provider: synthetic_account_number
        namespace: customer_identity
        cardinality_mode: deterministic_map
      - name: email
        strategy: synthetic_email
        provider: person_email
        namespace: customer_identity
        coherent_with: [first_name, last_name]
      - name: first_name
        strategy: replace_with_synthetic
        provider: person_first_name
      - name: last_name
        strategy: replace_with_synthetic
        provider: person_last_name
  - name: orders
    columns:
      - name: order_id
        strategy: preserve_format_id
        provider: uuid
      - name: customer_id
        strategy: from_parent
relationships:
  - parent:
      table: customers
      columns: [customer_id]
    children:
      - table: orders
        columns: [customer_id]
    orphan_policy: fail
    namespace: customer_identity
targets:
  customers:
    type: file
    format: csv
    path: ./out/customers_masked.csv
  orders:
    type: file
    format: csv
    path: ./out/orders_masked.csv
"""


def base_config() -> dict[str, Any]:
    """Return a fresh dict parsed from the advisory sketch."""
    return yaml.safe_load(ADVISORY_SKETCH_YAML)


# -- Positive path -----------------------------------------------------


class TestAdvisorySketchValidates:
    def test_advisory_sketch_validates_clean(self) -> None:
        cfg = PipelineConfig.model_validate(base_config())
        assert cfg.version == 1
        assert cfg.global_settings.seed == 42
        assert cfg.global_settings.post_validation is False
        assert len(cfg.tables) == 2
        assert len(cfg.relationships) == 1
        assert cfg.relationships[0].orphan_policy == "fail"


# -- Round-trip determinism (§4.5) ------------------------------------


class TestRoundTrip:
    def test_advisory_sketch_round_trips(self) -> None:
        parsed = base_config()
        dumped = PipelineConfig.model_validate(parsed).model_dump()
        re_validated = PipelineConfig.model_validate(dumped).model_dump()
        assert dumped == re_validated

    def test_dumped_config_contains_defaults(self) -> None:
        """Optional fields default to empty containers; model_dump() emits them."""
        dumped = PipelineConfig.model_validate(base_config()).model_dump()
        # The 'first_name' column doesn't declare namespace/cardinality_mode/coherent_with.
        first_name_col = dumped["tables"][0]["columns"][2]
        assert first_name_col["name"] == "first_name"
        assert first_name_col["namespace"] is None
        assert first_name_col["cardinality_mode"] is None
        assert first_name_col["coherent_with"] == []
        assert first_name_col["provider_config"] == {}


# -- Negative tests (§4.3) --------------------------------------------


class TestStrictValidationCatchesTypos:
    def test_typo_in_cardinality_mode_fails_loud(self) -> None:
        """Per axis 4 (strict) + correctness-contract §7 (no silent fallback)."""
        bad = base_config()
        bad["tables"][0]["columns"][0]["cardinality_mode"] = "uniqe"  # typo
        with pytest.raises(ValidationError, match="cardinality_mode"):
            PipelineConfig.model_validate(bad)

    def test_missing_orphan_policy_fails_loud(self) -> None:
        """S2 TODO 4 resolution: orphan_policy required; no default."""
        bad = base_config()
        del bad["relationships"][0]["orphan_policy"]
        with pytest.raises(ValidationError, match="orphan_policy"):
            PipelineConfig.model_validate(bad)

    def test_invalid_orphan_policy_fails_loud(self) -> None:
        """The four valid values are pinned by Literal."""
        bad = base_config()
        bad["relationships"][0]["orphan_policy"] = "vaporize_into_thin_air"
        with pytest.raises(ValidationError, match="orphan_policy"):
            PipelineConfig.model_validate(bad)

    def test_unknown_source_type_fails_loud(self) -> None:
        """V1 SourceDescriptor union is file-only; S3/GCS/SFTP are V2."""
        bad = base_config()
        bad["sources"]["customers"]["type"] = "ftp"  # not in V1 union
        with pytest.raises(ValidationError, match="type"):
            PipelineConfig.model_validate(bad)

    def test_unknown_top_level_key_fails_loud(self) -> None:
        """Axis 4 ratchet: extra='forbid' at the root catches unknown sections."""
        bad = base_config()
        bad["pipelines"] = [{"some": "thing"}]  # axis 3=A: no multi-pipeline files
        with pytest.raises(ValidationError, match="pipelines"):
            PipelineConfig.model_validate(bad)

    def test_v1_graph_mode_keys_rejected(self) -> None:
        """Axis 6: no V1 graph-mode compat; nodes/edges/mode rejected."""
        bad = base_config()
        bad["nodes"] = [{"id": "n1"}]
        with pytest.raises(ValidationError, match="nodes"):
            PipelineConfig.model_validate(bad)

    def test_unknown_source_format_fails_loud(self) -> None:
        """FileSource.format is Literal['csv', 'parquet'] only."""
        bad = base_config()
        bad["sources"]["customers"]["format"] = "tsv"
        with pytest.raises(ValidationError, match="format"):
            PipelineConfig.model_validate(bad)

    def test_unknown_column_key_fails_loud(self) -> None:
        """Per-column extra='forbid' catches typo'd column-level keys."""
        bad = base_config()
        bad["tables"][0]["columns"][0]["unknown_column_field"] = "x"
        with pytest.raises(ValidationError, match="unknown_column_field"):
            PipelineConfig.model_validate(bad)

    def test_empty_relationship_children_fails_loud(self) -> None:
        bad = base_config()
        bad["relationships"][0]["children"] = []
        with pytest.raises(ValidationError, match="children"):
            PipelineConfig.model_validate(bad)

    def test_empty_columns_on_relationship_end_fails_loud(self) -> None:
        bad = base_config()
        bad["relationships"][0]["parent"]["columns"] = []
        with pytest.raises(ValidationError, match="columns"):
            PipelineConfig.model_validate(bad)

    def test_wrong_version_fails_loud(self) -> None:
        """version: Literal[1] -- bumping is a deliberate schema change."""
        bad = base_config()
        bad["version"] = 2
        with pytest.raises(ValidationError, match="version"):
            PipelineConfig.model_validate(bad)


# -- override_sources -------------------------------------------------


class TestOverrideSources:
    def test_full_override_returns_new_dict_with_new_sources(self) -> None:
        cfg = PipelineConfig.model_validate(base_config()).model_dump()
        new_sources = {
            "customers": {
                "type": "file",
                "format": "csv",
                "path": "/runtime/customers_v2.csv",
            },
            "orders": {
                "type": "file",
                "format": "csv",
                "path": "/runtime/orders_v2.csv",
            },
        }
        result = override_sources(cfg, sources=new_sources)
        assert result["sources"]["customers"]["path"] == "/runtime/customers_v2.csv"
        assert result["sources"]["orders"]["path"] == "/runtime/orders_v2.csv"
        # Everything else unchanged.
        assert result["tables"] == cfg["tables"]
        assert result["relationships"] == cfg["relationships"]
        assert result["targets"] == cfg["targets"]
        assert result["global_settings"] == cfg["global_settings"]
        assert result["version"] == cfg["version"]

    def test_override_does_not_mutate_input(self) -> None:
        cfg = PipelineConfig.model_validate(base_config()).model_dump()
        snapshot = copy.deepcopy(cfg)
        new_sources = {
            "customers": {"type": "file", "format": "csv", "path": "/x.csv"},
            "orders": {"type": "file", "format": "csv", "path": "/y.csv"},
        }
        override_sources(cfg, sources=new_sources)
        assert cfg == snapshot

    def test_override_missing_table_binding_raises(self) -> None:
        cfg = PipelineConfig.model_validate(base_config()).model_dump()
        # orders binding missing.
        with pytest.raises(PipelineConfigError, match="missing source binding"):
            override_sources(
                cfg,
                sources={
                    "customers": {"type": "file", "format": "csv", "path": "/x.csv"},
                },
            )

    def test_override_extra_source_raises(self) -> None:
        cfg = PipelineConfig.model_validate(base_config()).model_dump()
        with pytest.raises(PipelineConfigError, match="extra source keys"):
            override_sources(
                cfg,
                sources={
                    "customers": {"type": "file", "format": "csv", "path": "/x.csv"},
                    "orders": {"type": "file", "format": "csv", "path": "/y.csv"},
                    "ghost_table": {"type": "file", "format": "csv", "path": "/z.csv"},
                },
            )

    def test_override_invalid_source_value_raises(self) -> None:
        cfg = PipelineConfig.model_validate(base_config()).model_dump()
        with pytest.raises(PipelineConfigError, match="strict validation"):
            override_sources(
                cfg,
                sources={
                    "customers": {"type": "ftp", "format": "csv", "path": "/x.csv"},
                    "orders": {"type": "file", "format": "csv", "path": "/y.csv"},
                },
            )

    def test_override_round_trips_through_validation(self) -> None:
        """Sanity: the returned dict re-validates clean."""
        cfg = PipelineConfig.model_validate(base_config()).model_dump()
        result = override_sources(
            cfg,
            sources={
                "customers": {"type": "file", "format": "csv", "path": "/x.csv"},
                "orders": {"type": "file", "format": "csv", "path": "/y.csv"},
            },
        )
        PipelineConfig.model_validate(result)  # must not raise
