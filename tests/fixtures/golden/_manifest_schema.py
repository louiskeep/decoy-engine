"""Pydantic schema for golden fixture manifests.

Every fixture under `tests/fixtures/golden/<name>/` ships a `manifest.yaml`
that declares its tables, files, relationships (if any), post-mask
invariants, and the sprints that gate on it. This module defines the
Pydantic model that validates each manifest at test time.

The `orphan_policy` field on `RelationshipEntry` is required (no
default) per the engine-v2 S2 spec TODO 4 resolution (resolution of
S2 B2): every declared FK relationship must explicitly name its
orphan-handling policy. Missing or invalid values fail compile in S2;
fail schema validation here.

Source pattern: shape draws from dbt's manifest.json schema (manifest
as an immutable artifact validated at the package boundary), Pydantic
docs convention `model_config = ConfigDict(extra="forbid")` to guard
against silent field drift, and `Literal[...]` for closed enums per
PEP 586.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OrphanPolicyLiteral = Literal["preserve", "remap", "warn", "fail"]
FileFormatLiteral = Literal["csv", "parquet"]


class FileEntry(BaseModel):
    """One source-data file in a fixture."""

    model_config = ConfigDict(extra="forbid")

    table: str
    path: str
    format: FileFormatLiteral


class RelationshipEnd(BaseModel):
    """One side of a relationship (parent or child).

    `columns` is a list (length 1 for single-column FKs; length >1 for
    composite-key FKs). Matches the engine-v2 S1 Relationship dataclass
    and the S2 RelationshipEdge shape.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    columns: list[str] = Field(min_length=1)


class RelationshipEntry(BaseModel):
    """One FK relationship in a fixture: parent + one or more children.

    `orphan_policy` is required (resolution of S2 B2; matches the
    `orphan_fk_policy_completeness` plan-compile check from S2). The
    four valid values are documented in S2 spec section 3.
    """

    model_config = ConfigDict(extra="forbid")

    parent: RelationshipEnd
    children: list[RelationshipEnd] = Field(min_length=1)
    orphan_policy: OrphanPolicyLiteral
    namespace: str | None = None


class FixtureManifest(BaseModel):
    """The full manifest.yaml schema.

    Every fixture under `tests/fixtures/golden/<name>/` validates against
    this model. `relationships` is allowed to be empty for single-table
    fixtures (e.g. `dirty_data/`, `repeated_within_column/`).
    """

    model_config = ConfigDict(extra="forbid")

    fixture_name: str
    description: str
    files: list[FileEntry] = Field(min_length=1)
    relationships: list[RelationshipEntry] = Field(default_factory=list)
    invariants_post_mask: list[str] = Field(default_factory=list)
    expected_orphans: int = 0
    gating_sprints: list[str] = Field(default_factory=list)
