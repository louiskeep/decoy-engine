"""Frozen dataclasses defining the Plan shape.

Plan is the planner's output: a versioned, audit-grade description of
how a pipeline will execute. Same input (config, profile, engine_version)
produces a byte-identical Plan; that's the S1 determinism contract.

All types frozen, all collections tuples (not lists). The plan is
read-only by construction; mutation goes through a new compile.

The YAML serialization shape (what gets written into a job manifest)
sits next to the dataclass shape via `_serialize.py`. Both forms hold
the same content; the dataclass is what S2-S13 consume in-engine, the
YAML is what gets archived for audit + replay.

Cardinality mode literals per S1 spec §2.
Orphan policy literals per S2 spec §3 TODO 4 (no default).
Backend type literals per S1 stub registry distinction (resolution of B3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CardinalityMode = Literal[
    "reuse",
    "unique",
    "match_source_cardinality",
    "scale_source_cardinality",
    "deterministic_map",
]

OrphanPolicy = Literal["preserve", "remap", "warn", "fail"]

BackendType = Literal["faker", "mimesis", "pool", "decoy_native"]


# ---------------------------------------------------------------------
# Seed envelope
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnSeed:
    """Per-column seed material + masking strategy.

    `column_seed` is derived via HKDF-SHA256 (RFC 5869) from `table_seed`
    + column name in S3 (Determinism Layer). S1 stubs the derivation as
    a placeholder integer; the `seed_protocol_version: 0` marker on the
    enclosing Plan signals to S3+ readers that this material is not
    real cryptographic key material.
    """

    column_seed: int
    namespace: str | None
    strategy: str
    provider: str
    backend_type: BackendType
    backend_version: str
    cardinality_mode: CardinalityMode
    provider_config: tuple[tuple[str, Any], ...] = field(default_factory=tuple)
    coherent_with: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GroupSeed:
    """Per-composite-group seed material (resolution of S2 spec B2).

    A composite FK column tuple gets one GroupSeed for the whole tuple
    instead of N independent ColumnSeed entries. The key under
    `per_group` is the canonical-joined column name (sorted column names
    joined with `__`).
    """

    group_seed: int
    namespace: str
    coherent_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableSeed:
    table_seed: int
    per_column: tuple[tuple[str, ColumnSeed], ...] = field(default_factory=tuple)
    per_group: tuple[tuple[str, GroupSeed], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SeedEnvelope:
    """Top-level seed material.

    S1 ships the structural envelope with a placeholder per-column seed
    derivation; the enclosing Plan stamps `seed_protocol_version: 0`.
    S3 replaces the derivation with real HMAC-keyed material and bumps
    `seed_protocol_version` to 1.
    """

    job_seed: int
    per_table: tuple[tuple[str, TableSeed], ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------
# Relationships + namespaces + ordering
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class PlanRelationshipEnd:
    """One side of a FK relationship in the Plan.

    Mirrors `decoy_engine.profile.Relationship` parent/child structure
    but at the planner layer: `namespace` is always resolved (non-None
    at this point because S2's `build_namespace_registry` runs before
    the relationship lands in the Plan).
    """

    table: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class PlanRelationship:
    """One FK relationship in the Plan, with orphan_policy resolved."""

    parent: PlanRelationshipEnd
    children: tuple[PlanRelationshipEnd, ...]
    orphan_policy: OrphanPolicy
    namespace: str | None  # may be None for unnamed FK relationships

    def __post_init__(self) -> None:
        if not self.children:
            raise ValueError(
                f"PlanRelationship {self.parent.table}.{self.parent.columns}: "
                "children must be non-empty."
            )
        parent_len = len(self.parent.columns)
        if parent_len == 0:
            raise ValueError(f"PlanRelationship: parent {self.parent.table} has empty columns.")
        for child in self.children:
            if len(child.columns) != parent_len:
                raise ValueError(
                    f"PlanRelationship {self.parent.table}.{self.parent.columns} -> "
                    f"{child.table}.{child.columns}: parent columns length "
                    f"{parent_len} != child columns length {len(child.columns)}. "
                    "Composite FK relationships require matching column tuples on "
                    "both sides (S1 check composite_columns_length_match)."
                )


@dataclass(frozen=True)
class NamespaceBinding:
    """One namespace and the (table, columns) tuples bound to it."""

    namespace: str
    declared_by: tuple[tuple[str, tuple[str, ...]], ...]
    seed: int


@dataclass(frozen=True)
class OrderingNode:
    """One step in the topologically-ordered mask plan.

    Composite parents are a single node (whole column tuple); children
    fire after every parent node they depend on.
    """

    table: str
    columns: tuple[str, ...]


# ---------------------------------------------------------------------
# Compile result + top-level Plan
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class PlanCompileResult:
    """Result block embedded in every Plan.

    `checks_passed` is the list of compile-time checks that ran and
    passed in this compile. `checks_skipped` is the list of profile-
    dependent checks that did NOT run (e.g. when --no-profile was set;
    resolution of S1 spec review H2). `warnings` and `errors` carry
    non-fatal observations (errors here are recoverable; fatal errors
    raise `PlanCompileError` directly).
    """

    checks_passed: tuple[str, ...] = field(default_factory=tuple)
    checks_skipped: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Plan:
    """Versioned plan artifact: the output of `compile_plan`.

    Frozen dataclass; same compile input produces a byte-identical Plan.
    The `seed_protocol_version` field is the S1-S3 transition marker:
    S1 stamps `0` (placeholder seed derivation); S3 bumps to `1` when
    real HMAC-keyed material lands. Any manifest carrying
    `seed_protocol_version: 0` is a development-era artifact that cannot
    be reproduced by post-S3 builds (per S1 spec H1 + done-definition
    release-notes rule).
    """

    plan_version: int
    seed_protocol_version: int
    engine_version: str
    pipeline_config_hash: str
    profile_hash: str
    seed_envelope: SeedEnvelope
    relationships: tuple[PlanRelationship, ...]
    namespaces: tuple[NamespaceBinding, ...]
    ordering: tuple[OrderingNode, ...]
    plan_compile: PlanCompileResult
