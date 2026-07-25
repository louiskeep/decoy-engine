"""RelationshipConfig: FK relationships at the pipeline-config layer.

Mirrors the S2 spec §3 relationship shape (resolution of S2 B2:
composite via `columns: tuple[str, ...]`). `orphan_policy` is the
closed Literal from S2 TODO 4 (no default; explicit required).

This is the INPUT-side relationship type. The OUTPUT-side
`PlanRelationship` (in `decoy_engine.plan._types`) is the post-compile
type with `orphan_policy` always set + `namespace` resolved by the
build_namespace_registry step. They share field names by design;
PlanRelationship adds invariants (composite_columns_length_match
__post_init__).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OrphanPolicyLiteral = Literal["preserve", "remap", "warn", "fail"]


class RelationshipEnd(BaseModel):
    """One side of a relationship (parent or child).

    `columns` is a list (length 1 for single-column FKs; length >1 for
    composite-key FKs). Mirrors the S2 B2 resolution exactly.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    columns: list[str] = Field(min_length=1)


class RelationshipConfig(BaseModel):
    """One FK relationship: parent + one or more children + policy + namespace.

    `orphan_policy` is required (S2 TODO 4: no default). The four valid
    values are documented in S2 spec §3.
    """

    model_config = ConfigDict(extra="forbid")

    parent: RelationshipEnd
    children: list[RelationshipEnd] = Field(min_length=1)
    orphan_policy: OrphanPolicyLiteral
    namespace: str | None = None
