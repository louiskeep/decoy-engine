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

# Post-S5 R6 reshape (per spec §6 + cross-sprint contracts §2.4 + R6):
# `deterministic_map` is REMOVED from the cardinality enumeration. The
# conflated "deterministic-vs-random" semantic moved to a sibling
# `deterministic: bool` plan field (see Plan/PlanRelationship). The two
# fields compose orthogonally; the 2x4 matrix lives in S5 spec §6.
# Plan-compile raises `plan_schema_deterministic_map_renamed` on any
# config still using the legacy keyword (pre-GA hard delete per
# best-practices §8.1).
CardinalityMode = Literal[
    "reuse",
    "unique",
    "match_source_cardinality",
    "scale_source_cardinality",
]

OrphanPolicy = Literal["preserve", "remap", "warn", "fail"]

BackendType = Literal["faker", "mimesis", "pool", "decoy_native"]


# ---------------------------------------------------------------------
# Seed envelope
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnSeed:
    """Per-column masking strategy + namespace binding.

    Post-S3 (per spec §5.5 plan-schema delta): no per-column seed integer.
    Determinism is fully expressed by
    `derive(plan.seed_envelope.job_seed, namespace, source_bytes)`;
    per-column distinctness comes from the namespace string + the source
    bytes, not from a per-column seed.

    Post-S5 (per spec §6 + R6 reshape): `deterministic` is a first-class
    per-column field. Composes orthogonally with `cardinality_mode` (the
    2x4 matrix in S5 spec §6). Defaults to False when the plan YAML
    omits the field; plan-compile fails on legacy `cardinality_mode:
    deterministic_map` with a rename error directing to the new shape.
    """

    namespace: str | None
    strategy: str
    # `provider` is required for generator strategies (faker); scalar transforms
    # (hash/redact/truncate/...) have no provider and read their settings from
    # `provider_config`. None for those (D4: require a provider only for faker).
    provider: str | None
    backend_type: BackendType
    backend_version: str
    cardinality_mode: CardinalityMode
    deterministic: bool = False
    provider_config: tuple[tuple[str, Any], ...] = field(default_factory=tuple)
    coherent_with: tuple[str, ...] = field(default_factory=tuple)
    # MG-1 S1 (2026-06-01): GDPR-aware technique classification.
    # Drives the FE strategy-picker badge ("Pseudonymisation" /
    # "Anonymisation" / "Synthetic" / "Passthrough"). Set by
    # plan-compile from the central TECHNIQUE_CLASS_BY_STRATEGY map
    # (decoy_engine.execution._technique_class). None means
    # "unclassified" -- the FE renders a needs-review badge so a
    # newly-added strategy can't ship without an explicit label.
    technique_class: str | None = None


@dataclass(frozen=True)
class GroupSeed:
    """Per-composite-group namespace binding.

    A composite FK column tuple gets one GroupSeed for the whole tuple
    instead of N independent ColumnSeed entries. The key under
    `per_group` is the canonical-joined column name (sorted column names
    joined with `__`).

    Post-S3: no per-group seed integer; determinism keys off the
    namespace string + canonical-tuple source bytes via `derive(...)`.
    """

    namespace: str
    coherent_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableSeed:
    """Per-table grouping of ColumnSeed + GroupSeed entries.

    Post-S3: no `table_seed` integer; tables are not a separate axis in
    `derive(...)`. Namespace strings already encode the per-table-binding
    structure where it matters.
    """

    per_column: tuple[tuple[str, ColumnSeed], ...] = field(default_factory=tuple)
    per_group: tuple[tuple[str, GroupSeed], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SeedEnvelope:
    """Top-level seed material.

    Post-S3 (per spec §5.5): `job_seed` is `bytes` (exactly 8 bytes), not
    `int`. It is the sole entropy input to
    `decoy_engine.determinism.derive(...)`. The config-side `int` (or
    `str`, or absent) for `seed:` is normalized to bytes exactly once at
    the pipeline-config adapter boundary in `compile_plan`.
    """

    job_seed: bytes
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
    """One namespace and the (table, columns) tuples bound to it.

    Post-S3 (per spec §5.5): no `seed` int field. Namespace strings feed
    into `derive(...)` directly; per-namespace seed material is not
    stored.
    """

    namespace: str
    declared_by: tuple[tuple[str, tuple[str, ...]], ...]


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
    The `seed_protocol_version` field tracks the determinism-envelope
    version: S1 stamped `0` (placeholder seed derivation); S3 bumped to `1`
    when real HMAC-keyed material landed; the F-series corrections bumped to
    `2` (coordinated Faker-seeding + canonicalize-integer fixes that shift
    deterministic output). A manifest carrying an older version cannot be
    reproduced by builds at a newer version (per S1 spec H1 + done-definition
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
