"""Tests for profile_source orchestration.

Covers:
- Reading file/csv sources via the golden fixture suite (slice 4).
- PK / FK metadata derivation from relationships.
- Composite-key FK propagation.
- Multi-table composition.
- Audit signal: two source bindings -> two profile_hashes (Dennis handoff §4.4).
- Error paths: unsupported source type, missing path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decoy_engine.config import PipelineConfig, override_sources
from decoy_engine.profile import Profile, profile_hash, profile_source

GOLDEN_ROOT = Path(__file__).resolve().parent.parent.parent / "fixtures" / "golden"


def _relational_parent_child_config() -> dict:
    """A pipeline-config pointing at the relational_parent_child fixture."""
    return {
        "version": 1,
        "global_settings": {"seed": 42, "post_validation": False},
        "sources": {
            "customers": {
                "type": "file",
                "format": "csv",
                "path": str(GOLDEN_ROOT / "relational_parent_child" / "customers.csv"),
            },
            "orders": {
                "type": "file",
                "format": "csv",
                "path": str(GOLDEN_ROOT / "relational_parent_child" / "orders.csv"),
            },
        },
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {
                        "name": "customer_id",
                        "strategy": "preserve_format_id",
                        "provider": "uuid",
                    }
                ],
            },
            {
                "name": "orders",
                "columns": [
                    {
                        "name": "customer_id",
                        "strategy": "from_parent",
                    }
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["customer_id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "fail",
                "namespace": "customer_identity",
            }
        ],
        "targets": {
            "customers": {"type": "file", "format": "csv", "path": "/tmp/out_customers.csv"},
            "orders": {"type": "file", "format": "csv", "path": "/tmp/out_orders.csv"},
        },
    }


def _composite_key_config() -> dict:
    """A pipeline-config pointing at the composite_key fixture."""
    return {
        "version": 1,
        "global_settings": {"seed": 1, "post_validation": False},
        "sources": {
            "enrollments": {
                "type": "file",
                "format": "csv",
                "path": str(GOLDEN_ROOT / "composite_key" / "enrollments.csv"),
            },
            "claims": {
                "type": "file",
                "format": "csv",
                "path": str(GOLDEN_ROOT / "composite_key" / "claims.csv"),
            },
        },
        "tables": [
            {
                "name": "enrollments",
                "columns": [
                    {
                        "name": "member_id",
                        "strategy": "preserve_format_id",
                        "provider": "synthetic_member_id",
                    }
                ],
            },
            {
                "name": "claims",
                "columns": [{"name": "member_id", "strategy": "from_parent"}],
            },
        ],
        "relationships": [
            {
                "parent": {
                    "table": "enrollments",
                    "columns": ["member_id", "plan_id", "effective_date"],
                },
                "children": [
                    {
                        "table": "claims",
                        "columns": ["member_id", "plan_id", "effective_date"],
                    }
                ],
                "orphan_policy": "fail",
                "namespace": "enrollment_identity",
            }
        ],
        "targets": {
            "enrollments": {"type": "file", "format": "csv", "path": "/tmp/out_enrollments.csv"},
            "claims": {"type": "file", "format": "csv", "path": "/tmp/out_claims.csv"},
        },
    }


# -- Happy path ---------------------------------------------------------


class TestProfileSourceHappyPath:
    def test_relational_parent_child_produces_two_table_profiles(self) -> None:
        cfg = PipelineConfig.model_validate(_relational_parent_child_config()).model_dump()
        profile = profile_source(cfg, seed=42)
        assert isinstance(profile, Profile)
        table_names = {t.name for t in profile.tables}
        assert table_names == {"customers", "orders"}

    def test_row_counts_match_fixture(self) -> None:
        cfg = PipelineConfig.model_validate(_relational_parent_child_config()).model_dump()
        profile = profile_source(cfg, sample_rows=None, seed=42)  # full scan
        by_name = {t.name: t for t in profile.tables}
        assert by_name["customers"].row_count == 100
        assert by_name["orders"].row_count == 500

    def test_relationships_carried_through(self) -> None:
        cfg = PipelineConfig.model_validate(_relational_parent_child_config()).model_dump()
        profile = profile_source(cfg, seed=42)
        assert len(profile.relationships) == 1
        rel = profile.relationships[0]
        assert rel.parent_table == "customers"
        assert rel.parent_columns == ("customer_id",)
        assert rel.child_table == "orders"
        assert rel.child_columns == ("customer_id",)
        assert rel.namespace == "customer_identity"


# -- PK / FK derivation --------------------------------------------------


class TestPkFkDerivation:
    def test_pk_derived_from_parent_columns(self) -> None:
        cfg = PipelineConfig.model_validate(_relational_parent_child_config()).model_dump()
        profile = profile_source(cfg, seed=42)
        customers = next(t for t in profile.tables if t.name == "customers")
        customer_id = next(c for c in customers.columns if c.name == "customer_id")
        assert customer_id.declared_pk is True

    def test_fk_derived_from_child_columns(self) -> None:
        cfg = PipelineConfig.model_validate(_relational_parent_child_config()).model_dump()
        profile = profile_source(cfg, seed=42)
        orders = next(t for t in profile.tables if t.name == "orders")
        customer_id = next(c for c in orders.columns if c.name == "customer_id")
        assert customer_id.is_fk is True
        assert customer_id.fk_target == ("customers", "customer_id")

    def test_non_pk_non_fk_column(self) -> None:
        cfg = PipelineConfig.model_validate(_relational_parent_child_config()).model_dump()
        profile = profile_source(cfg, seed=42)
        customers = next(t for t in profile.tables if t.name == "customers")
        name_col = next(c for c in customers.columns if c.name == "name")
        assert name_col.declared_pk is False
        assert name_col.is_fk is False
        assert name_col.fk_target is None


# -- Composite-key handling ---------------------------------------------


class TestCompositeKeyDerivation:
    def test_composite_pk_marks_all_member_columns(self) -> None:
        cfg = PipelineConfig.model_validate(_composite_key_config()).model_dump()
        profile = profile_source(cfg, sample_rows=None, seed=1)
        enrollments = next(t for t in profile.tables if t.name == "enrollments")
        by_name = {c.name: c for c in enrollments.columns}
        # All three composite-PK members marked declared_pk.
        assert by_name["member_id"].declared_pk is True
        assert by_name["plan_id"].declared_pk is True
        assert by_name["effective_date"].declared_pk is True

    def test_composite_fk_positional_mapping(self) -> None:
        cfg = PipelineConfig.model_validate(_composite_key_config()).model_dump()
        profile = profile_source(cfg, sample_rows=None, seed=1)
        claims = next(t for t in profile.tables if t.name == "claims")
        by_name = {c.name: c for c in claims.columns}
        # Each composite-FK child column maps to its positional parent column.
        assert by_name["member_id"].fk_target == ("enrollments", "member_id")
        assert by_name["plan_id"].fk_target == ("enrollments", "plan_id")
        assert by_name["effective_date"].fk_target == ("enrollments", "effective_date")

    def test_composite_relationship_carried_through(self) -> None:
        cfg = PipelineConfig.model_validate(_composite_key_config()).model_dump()
        profile = profile_source(cfg, sample_rows=None, seed=1)
        assert len(profile.relationships) == 1
        rel = profile.relationships[0]
        assert rel.parent_columns == ("member_id", "plan_id", "effective_date")
        assert rel.child_columns == ("member_id", "plan_id", "effective_date")


# -- Audit signal (Dennis handoff §4.4) ---------------------------------


class TestAuditSignal:
    def test_same_pipeline_two_sources_two_profile_hashes(self, tmp_path: Path) -> None:
        """The audit signal S1 spec promises: source-binding swap shows up
        as a profile_hash difference. If this fails, the job-time source-
        binding contract is broken."""
        # Build two source bindings that point at different fixture pairs.
        cfg = PipelineConfig.model_validate(_relational_parent_child_config()).model_dump()
        sources_a = cfg["sources"]
        sources_b = {
            "customers": {
                "type": "file",
                "format": "csv",
                "path": str(GOLDEN_ROOT / "orphan_fk" / "customers.csv"),  # 50 rows, not 100
            },
            "orders": {
                "type": "file",
                "format": "csv",
                "path": str(GOLDEN_ROOT / "orphan_fk" / "orders.csv"),  # 100 rows, not 500
            },
        }
        cfg_a = override_sources(cfg, sources=sources_a)
        cfg_b = override_sources(cfg, sources=sources_b)
        profile_a = profile_source(cfg_a, sample_rows=None, seed=42)
        profile_b = profile_source(cfg_b, sample_rows=None, seed=42)
        # Profile hashes differ because the source data shape (row counts +
        # distinct counts) differs.
        assert profile_hash(profile_a) != profile_hash(profile_b)
        # But structural fields (relationships) are identical because they
        # come from config, not from source data.
        assert profile_a.relationships == profile_b.relationships


# -- Error paths --------------------------------------------------------


class TestErrorPaths:
    def test_unsupported_source_type_raises(self, tmp_path: Path) -> None:
        """Defensive: a caller that skipped the adapter and built a config
        with an unknown source type lands here with NotImplementedError."""
        cfg = {
            "sources": {"x": {"type": "ftp", "format": "csv", "path": "/x.csv"}},
            "relationships": [],
        }
        with pytest.raises(NotImplementedError, match="ftp"):
            profile_source(cfg, seed=1)

    def test_unsupported_file_format_raises(self, tmp_path: Path) -> None:
        cfg = {
            "sources": {"x": {"type": "file", "format": "tsv", "path": "/x.tsv"}},
            "relationships": [],
        }
        with pytest.raises(NotImplementedError, match="tsv"):
            profile_source(cfg, seed=1)

    def test_missing_path_raises(self, tmp_path: Path) -> None:
        cfg = {
            "sources": {"x": {"type": "file", "format": "csv"}},  # no path
            "relationships": [],
        }
        with pytest.raises(ValueError, match="path"):
            profile_source(cfg, seed=1)


# -- Determinism --------------------------------------------------------


class TestDeterminism:
    def test_same_seed_same_profile_hash(self) -> None:
        cfg = PipelineConfig.model_validate(_relational_parent_child_config()).model_dump()
        p1 = profile_source(cfg, sample_rows=None, seed=42)
        p2 = profile_source(cfg, sample_rows=None, seed=42)
        # Full-scan + same seed: data-shape fields are identical, so hash matches.
        assert profile_hash(p1) == profile_hash(p2)

    def test_profile_seed_recorded_in_sidecar(self) -> None:
        cfg = PipelineConfig.model_validate(_relational_parent_child_config()).model_dump()
        profile = profile_source(cfg, seed=42)
        assert profile.profile_seed == 42

    def test_no_seed_records_none(self) -> None:
        cfg = PipelineConfig.model_validate(_relational_parent_child_config()).model_dump()
        profile = profile_source(cfg, seed=None)
        assert profile.profile_seed is None
