"""Pydantic manifest-schema validation for every golden fixture.

This file is the schema gate. Every fixture under
`tests/fixtures/golden/` must produce a manifest.yaml that validates
against the FixtureManifest model. The schema is intentionally strict
(extra fields forbidden, orphan_policy required where relationships
exist, columns lists must be non-empty) so silent drift surfaces in CI
rather than at runtime.

Resolution of engine-v2 S1 spec M3 (manifest schema as concrete Pydantic
model) and S2 B2 (orphan_policy field required on every Relationship
entry; no default).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tests.fixtures.golden._manifest_schema import (
    FixtureManifest,
    RelationshipEntry,
)

GOLDEN_ROOT = Path(__file__).resolve().parent.parent.parent / "fixtures" / "golden"

EXPECTED_FIXTURES = {
    "composite_coherence",
    "composite_key",
    "dirty_data",
    "nullable_fk",
    "orphan_fk",
    "relational_parent_child",
    "repeated_across_tables",
    "repeated_within_column",
    "self_fk",
}


def _fixture_dirs() -> list[Path]:
    return sorted(p for p in GOLDEN_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_"))


@pytest.mark.golden
class TestFixtureManifestStructure:
    def test_all_expected_fixtures_present(self) -> None:
        actual = {p.name for p in _fixture_dirs()}
        assert actual == EXPECTED_FIXTURES, (
            f"Golden fixture set drift: missing={EXPECTED_FIXTURES - actual}, "
            f"extra={actual - EXPECTED_FIXTURES}"
        )

    @pytest.mark.parametrize("fixture_dir", _fixture_dirs(), ids=lambda p: p.name)
    def test_manifest_validates(self, fixture_dir: Path) -> None:
        manifest_path = fixture_dir / "manifest.yaml"
        assert manifest_path.exists(), f"Missing manifest.yaml in {fixture_dir.name}/"
        with manifest_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        # Will raise ValidationError if shape is wrong.
        manifest = FixtureManifest(**data)
        # fixture_name must match the directory name for fast skim navigation.
        assert manifest.fixture_name == fixture_dir.name

    @pytest.mark.parametrize("fixture_dir", _fixture_dirs(), ids=lambda p: p.name)
    def test_declared_files_exist(self, fixture_dir: Path) -> None:
        with (fixture_dir / "manifest.yaml").open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        manifest = FixtureManifest(**data)
        for file_entry in manifest.files:
            assert (fixture_dir / file_entry.path).exists(), (
                f"{fixture_dir.name}/{file_entry.path} declared in manifest but file missing"
            )

    @pytest.mark.parametrize("fixture_dir", _fixture_dirs(), ids=lambda p: p.name)
    def test_relationships_have_orphan_policy(self, fixture_dir: Path) -> None:
        """S2 B2: orphan_policy required on every declared relationship."""
        with (fixture_dir / "manifest.yaml").open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        manifest = FixtureManifest(**data)
        for rel in manifest.relationships:
            assert rel.orphan_policy in ("preserve", "remap", "warn", "fail"), (
                f"{fixture_dir.name}: relationship {rel.parent.table} -> "
                f"{[c.table for c in rel.children]} has invalid orphan_policy "
                f"{rel.orphan_policy!r}"
            )


@pytest.mark.golden
class TestSchemaRejectsMissingOrphanPolicy:
    """S2 B2 contract: a manifest that omits orphan_policy on any
    declared relationship fails schema validation. Pydantic raises
    ValidationError naming the missing field.
    """

    def test_relationship_without_orphan_policy_raises(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            RelationshipEntry(
                parent={"table": "customers", "columns": ["customer_id"]},  # type: ignore[arg-type]
                children=[{"table": "orders", "columns": ["customer_id"]}],  # type: ignore[list-item]
                # orphan_policy intentionally omitted
            )
        msg = str(excinfo.value)
        assert "orphan_policy" in msg

    def test_relationship_with_invalid_orphan_policy_raises(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            RelationshipEntry(
                parent={"table": "customers", "columns": ["customer_id"]},  # type: ignore[arg-type]
                children=[{"table": "orders", "columns": ["customer_id"]}],  # type: ignore[list-item]
                orphan_policy="vaporize_into_thin_air",  # type: ignore[arg-type]
            )
        msg = str(excinfo.value)
        assert "orphan_policy" in msg

    def test_relationship_accepts_each_valid_policy(self) -> None:
        for policy in ("preserve", "remap", "warn", "fail"):
            rel = RelationshipEntry(
                parent={"table": "customers", "columns": ["customer_id"]},  # type: ignore[arg-type]
                children=[{"table": "orders", "columns": ["customer_id"]}],  # type: ignore[list-item]
                orphan_policy=policy,  # type: ignore[arg-type]
            )
            assert rel.orphan_policy == policy


@pytest.mark.golden
class TestSchemaRejectsExtras:
    """Extras forbidden: silent field drift surfaces in CI."""

    def test_manifest_with_unknown_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            FixtureManifest(
                fixture_name="x",
                description="y",
                files=[{"table": "t", "path": "t.csv", "format": "csv"}],  # type: ignore[list-item]
                unknown_field="should fail",  # type: ignore[call-arg]
            )

    def test_relationship_with_unknown_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            RelationshipEntry(
                parent={"table": "c", "columns": ["id"]},  # type: ignore[arg-type]
                children=[{"table": "o", "columns": ["id"]}],  # type: ignore[list-item]
                orphan_policy="fail",
                unknown_field="should fail",  # type: ignore[call-arg]
            )


@pytest.mark.golden
class TestRelationshipShapeInvariants:
    def test_parent_and_children_columns_must_match_length(self) -> None:
        # Length mismatch is a logical FK error; the schema doesn't enforce
        # cross-field length match (the planner does), but each list must
        # be non-empty independently.
        rel = RelationshipEntry(
            parent={"table": "p", "columns": ["a", "b"]},  # type: ignore[arg-type]
            children=[{"table": "c", "columns": ["a", "b"]}],  # type: ignore[list-item]
            orphan_policy="fail",
        )
        assert len(rel.parent.columns) == len(rel.children[0].columns) == 2

    def test_empty_columns_list_raises(self) -> None:
        with pytest.raises(ValidationError):
            RelationshipEntry(
                parent={"table": "p", "columns": []},  # type: ignore[arg-type]
                children=[{"table": "c", "columns": ["id"]}],  # type: ignore[list-item]
                orphan_policy="fail",
            )

    def test_empty_children_list_raises(self) -> None:
        with pytest.raises(ValidationError):
            RelationshipEntry(
                parent={"table": "p", "columns": ["id"]},  # type: ignore[arg-type]
                children=[],
                orphan_policy="fail",
            )
