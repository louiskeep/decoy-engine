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
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from decoy_engine.config import PipelineConfig, PipelineConfigError, override_sources
from decoy_engine.plan import Plan, compile_plan
from decoy_engine.profile import Profile, profile_source

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
        deterministic: true
        cardinality_mode: reuse
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
namespaces:
  customer_identity:
    declared_by: [customers.customer_id, customers.email]
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
        # R6 reshape fields default to False / None when omitted.
        assert first_name_col["deterministic"] is False
        assert first_name_col["scale"] is None


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

    def test_legacy_deterministic_map_rejected(self) -> None:
        """R6 reshape: `deterministic_map` was removed from the cardinality
        Literal. The adapter rejects it (the engine also raises a migration
        error on the raw-dict path), so it never silently slips through."""
        bad = base_config()
        bad["tables"][0]["columns"][0]["cardinality_mode"] = "deterministic_map"
        with pytest.raises(ValidationError, match="cardinality_mode"):
            PipelineConfig.model_validate(bad)

    def test_unknown_on_pool_exhaustion_fails_loud(self) -> None:
        """global_settings.on_pool_exhaustion is a closed Literal
        (fail | scale_up | fall_back); an unknown value is caught at the door."""
        bad = base_config()
        bad["global_settings"]["on_pool_exhaustion"] = "explode"
        with pytest.raises(ValidationError, match="on_pool_exhaustion"):
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


# -- Slice-1 acceptance: PipelineConfig -> profile_source -> compile_plan ----


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def _acceptance_config(tmp_path: Path) -> dict[str, Any]:
    """A current-engine-valid pipeline config over two tiny CSVs.

    Mirrors the compile-valid shape in tests/unit/plan/conftest.py::simple_config
    (faker_name / person_name, reuse mode, one FK relationship), plus the
    sources/targets PipelineConfig requires and the namespaces block. Used to
    prove the validated dict round-trips through profile_source + compile_plan.
    """
    src = tmp_path / "in"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    customers_csv = src / "customers.csv"
    orders_csv = src / "orders.csv"
    _write_csv(customers_csv, "customer_id,name", ["1,Alice", "2,Bob", "3,Carol"])
    _write_csv(orders_csv, "order_id,customer_id", ["10,1", "11,2", "12,1"])
    return {
        "version": 1,
        "global_settings": {"seed": 42, "on_pool_exhaustion": "scale_up"},
        "sources": {
            "customers": {"type": "file", "format": "csv", "path": str(customers_csv)},
            "orders": {"type": "file", "format": "csv", "path": str(orders_csv)},
        },
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {
                        "name": "name",
                        "strategy": "faker_name",
                        "provider": "person_name",
                        "cardinality_mode": "reuse",
                    }
                ],
            }
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["customer_id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "fail",
                "namespace": "customer_identity",
            }
        ],
        "namespaces": {
            "customer_identity": {"declared_by": ["customers.customer_id", "orders.customer_id"]}
        },
        "targets": {
            "customers": {"type": "file", "format": "csv", "path": str(out / "customers.csv")},
            "orders": {"type": "file", "format": "csv", "path": str(out / "orders.csv")},
        },
    }


class TestSliceOneAcceptance:
    """The slice-1 proof (Dennis S60): the validated dict round-trips through
    profile_source + compile_plan. This is the integration test that catches a
    config schema drifting from the engine's accepted config-dict contract (it
    would have failed on all four S2-S13 drifts the reshape fixes)."""

    def test_profile_source_builds_profile_with_both_tables_and_fk(self, tmp_path: Path) -> None:
        cfg = PipelineConfig.model_validate(_acceptance_config(tmp_path)).model_dump()
        profile = profile_source(cfg, seed=42)
        assert isinstance(profile, Profile)
        assert {t.name for t in profile.tables} == {"customers", "orders"}
        assert len(profile.relationships) == 1
        rel = profile.relationships[0]
        assert rel.parent_table == "customers"
        assert rel.child_table == "orders"
        assert rel.parent_columns == ("customer_id",)
        assert rel.child_columns == ("customer_id",)

    def test_validated_config_compiles_to_a_plan(self, tmp_path: Path) -> None:
        cfg = PipelineConfig.model_validate(_acceptance_config(tmp_path)).model_dump()
        profile = profile_source(cfg, seed=42)
        plan = compile_plan(cfg, profile, decoy_engine_version="0.1.0")
        assert isinstance(plan, Plan)


class TestQaWalksGenF6GenerateColumnConfigTypeParams:
    """QA walks/generators F6 (2026-06-01, MEDIUM correctness / PO
    Q-F6=no-users): every non-reference generator type must declare
    its required params at PipelineConfig validation time. Pre-fix a
    YAML typo (`fker_type: email`) passed validation silently because
    `extra="allow"` carried any key through; V1 + V2 then fell back to
    the `word` generator at generation time + produced wrong output
    with no operator-visible error.

    Per PO Q-F6=no-users: no in-the-wild manifests rely on the soft-
    fail behavior, so the validator raises hard. No deprecation
    migration route."""

    def _wrap(self, col: dict) -> dict:
        # FC-1 (2026-06-02): top-level `mode:` discriminator dropped.
        # Per-table kind is inferred from `generate_columns` presence.
        return {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {},
            "tables": [{"name": "t", "row_count": 5, "generate_columns": [col]}],
            "targets": {"t": {"type": "file", "format": "csv", "path": "o.csv"}},
        }

    def test_faker_missing_faker_type_raises(self):
        cfg = self._wrap({"name": "fk", "type": "faker"})
        with pytest.raises(ValidationError, match="faker column 'fk' requires `faker_type`"):
            PipelineConfig.model_validate(cfg)

    def test_faker_with_typo_raises(self):
        """The audit-named scenario: `fker_type` instead of `faker_type`.
        Pre-fix passed validation + fell back to V1 word generator at
        generation time."""
        cfg = self._wrap({"name": "fk", "type": "faker", "fker_type": "email"})
        with pytest.raises(ValidationError, match="faker column 'fk' requires `faker_type`"):
            PipelineConfig.model_validate(cfg)

    def test_sequence_missing_start_raises(self):
        cfg = self._wrap({"name": "id", "type": "sequence", "step": 1})
        with pytest.raises(ValidationError, match="sequence column 'id' requires `start`"):
            PipelineConfig.model_validate(cfg)

    def test_categorical_missing_categories_raises(self):
        cfg = self._wrap({"name": "dept", "type": "categorical"})
        with pytest.raises(
            ValidationError, match="categorical column 'dept' requires `categories`"
        ):
            PipelineConfig.model_validate(cfg)

    def test_categorical_empty_categories_raises(self):
        cfg = self._wrap({"name": "dept", "type": "categorical", "categories": []})
        with pytest.raises(
            ValidationError, match="categorical column 'dept' requires `categories`"
        ):
            PipelineConfig.model_validate(cfg)

    def test_formula_missing_formula_raises(self):
        cfg = self._wrap({"name": "x", "type": "formula"})
        with pytest.raises(ValidationError, match="formula column 'x' requires `formula`"):
            PipelineConfig.model_validate(cfg)

    def test_formula_empty_string_raises(self):
        cfg = self._wrap({"name": "x", "type": "formula", "formula": ""})
        with pytest.raises(ValidationError, match="formula column 'x' requires `formula`"):
            PipelineConfig.model_validate(cfg)

    def test_reference_validator_unchanged_still_raises_on_missing_table(self):
        """Existing _reference_params_required validator still works
        alongside the new _type_params_present validator."""
        cfg = self._wrap(
            {
                "name": "fk",
                "type": "reference",
                "reference_column": "id",
            }
        )
        with pytest.raises(
            ValidationError, match="reference column 'fk' requires `reference_table`"
        ):
            PipelineConfig.model_validate(cfg)

    def test_faker_with_unrelated_extra_still_validates(self):
        """`extra="allow"` stays in place (Dennis S6-ENG-1 gate
        Q-S6-1 lock). Unrelated extras like custom markers must still
        pass through. F6 catches MISSING-required-params, not
        UNKNOWN-extras."""
        cfg = self._wrap(
            {
                "name": "fk",
                "type": "faker",
                "faker_type": "email",
                "custom_marker": "team-alpha",
            }
        )
        validated = PipelineConfig.model_validate(cfg)
        col_dump = validated.model_dump()["tables"][0]["generate_columns"][0]
        assert col_dump.get("custom_marker") == "team-alpha"

    def test_sequence_with_explicit_start_zero_validates(self):
        """`start: 0` is a legitimate sequence start; the `is None`
        check (not falsy check) accepts it."""
        cfg = self._wrap({"name": "i", "type": "sequence", "start": 0})
        PipelineConfig.model_validate(cfg)  # must not raise
