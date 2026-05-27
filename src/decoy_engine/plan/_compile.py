"""compile_plan: the keystone S1 deliverable.

`compile_plan(config, profile, *, decoy_engine_version)` consumes a
parsed pipeline config + a Profile + the engine version stamp and
produces a frozen Plan. Pure function: same inputs -> byte-identical
output. Validation runs always (never flag-gated); failures raise
`PlanCompileError` with `code` + `path` + `message`.

S1 ships five foundational checks (per the compile-check ownership
table rows 1-5). S2-S9 add per-module rules following the same call
shape; the check-runner here is the slot they slot into.

Seed envelope derivation in S1 is a stub: each ColumnSeed gets a
deterministic-but-not-cryptographically-secure integer derived from
(job_seed, table_name, column_name). S3 replaces with real HKDF-SHA256
material per the spec; the enclosing Plan stamps
`seed_protocol_version: 0` for the S1 era.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from decoy_engine.plan._checks import (
    check_basic_uniqueness_pre_flight,
    check_composite_columns_length_match,
    check_fk_plan_ordering,
    check_namespace_ambiguity,
    check_unknown_provider,
)
from decoy_engine.plan._types import (
    ColumnSeed,
    GroupSeed,
    NamespaceBinding,
    OrderingNode,
    Plan,
    PlanCompileResult,
    PlanRelationship,
    PlanRelationshipEnd,
    SeedEnvelope,
    TableSeed,
)
from decoy_engine.profile._hash import profile_hash
from decoy_engine.profile._types import Profile

# S1's plan_version is 1. seed_protocol_version is 0 (S3 bumps to 1).
PLAN_VERSION = 1
SEED_PROTOCOL_VERSION = 0


def compile_plan(
    config: dict[str, Any],
    profile: Profile,
    *,
    decoy_engine_version: str,
) -> Plan:
    """Compile (config, profile, engine_version) into a frozen Plan.

    Raises:
        PlanCompileError: if any of the five S1 compile-time checks fails.
            The error carries `code`, `path`, and `message` for downstream
            UI rendering.

    Determinism contract: two calls with `__eq__`-equal inputs produce
    `__eq__`-equal Plans whose YAML serializations are byte-identical.
    """
    # Run the always-on checks. Each raises PlanCompileError on fail;
    # silence on pass means the check went into checks_passed.
    check_namespace_ambiguity(config)
    check_unknown_provider(config)
    check_composite_columns_length_match(profile)
    ordering_nodes = check_fk_plan_ordering(profile)
    check_basic_uniqueness_pre_flight(config, profile)

    checks_passed = (
        "namespace_ambiguity",
        "unknown_provider",
        "fk_plan_ordering",
        "basic_uniqueness_pre_flight",
        "composite_columns_length_match",
    )

    # Hashes.
    cfg_hash = _hash_config(config)
    prof_hash = profile_hash(profile)

    # Build the constituent blocks.
    relationships = _build_relationships(config, profile)
    namespaces = _build_namespaces(config)
    ordering = tuple(OrderingNode(table=t, columns=c) for (t, c) in ordering_nodes)
    seed_envelope = _build_seed_envelope_stub(config, profile)

    return Plan(
        plan_version=PLAN_VERSION,
        seed_protocol_version=SEED_PROTOCOL_VERSION,
        engine_version=decoy_engine_version,
        pipeline_config_hash=cfg_hash,
        profile_hash=prof_hash,
        seed_envelope=seed_envelope,
        relationships=relationships,
        namespaces=namespaces,
        ordering=ordering,
        plan_compile=PlanCompileResult(checks_passed=checks_passed),
    )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _hash_config(config: dict[str, Any]) -> str:
    """SHA-256 over a canonical JSON serialization of the config.

    Sort_keys=True, ensure_ascii=True, separators=(",", ":") for byte
    stability across Python runtimes. Same input config (regardless of
    key insertion order) produces the same hash.
    """
    canonical = json.dumps(
        config,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _build_relationships(config: dict[str, Any], profile: Profile) -> tuple[PlanRelationship, ...]:
    """Convert profile.relationships into Plan-side PlanRelationship tuples,
    pulling orphan_policy from the config when available.
    """
    # Build (parent_table, parent_columns) -> orphan_policy from config.
    orphan_policy_lookup: dict[tuple[str, tuple[str, ...]], str] = {}
    config_relationships = config.get("relationships", [])
    if isinstance(config_relationships, list):
        for entry in config_relationships:
            if not isinstance(entry, dict):
                continue
            parent = entry.get("parent")
            policy = entry.get("orphan_policy")
            if not isinstance(parent, dict) or not policy:
                continue
            parent_table = parent.get("table")
            parent_cols = parent.get("columns")
            if (
                isinstance(parent_table, str)
                and isinstance(parent_cols, list)
                and all(isinstance(c, str) for c in parent_cols)
            ):
                orphan_policy_lookup[(parent_table, tuple(parent_cols))] = policy

    # Group profile.relationships by (parent_table, parent_columns) so
    # multiple children of the same parent collapse into one
    # PlanRelationship entry.
    grouped: dict[
        tuple[str, tuple[str, ...]],
        list[tuple[str, tuple[str, ...], str | None]],
    ] = {}
    for rel in profile.relationships:
        key = (rel.parent_table, rel.parent_columns)
        grouped.setdefault(key, []).append((rel.child_table, rel.child_columns, rel.namespace))

    out: list[PlanRelationship] = []
    for (parent_table, parent_cols), children in sorted(grouped.items()):
        # All children of the same parent share a namespace if any do
        # (S2 enforces this at build_namespace_registry time; for S1 we
        # take the first non-None we see).
        namespace = next((ns for (_, _, ns) in children if ns is not None), None)
        # orphan_policy: lookup in config; fall back to "preserve" only
        # when the relationship is not declared in the config (S2's
        # orphan_fk_policy_completeness check then catches the omission
        # at the config layer). S1 doesn't ship that check yet.
        policy = orphan_policy_lookup.get((parent_table, parent_cols), "preserve")
        # Type the policy as the literal we accept.
        if policy not in ("preserve", "remap", "warn", "fail"):
            # Defensive: invalid policy in config gets caught here. S2 row 6
            # ships the proper compile error; S1 just normalizes.
            policy = "preserve"
        out.append(
            PlanRelationship(
                parent=PlanRelationshipEnd(table=parent_table, columns=parent_cols),
                children=tuple(PlanRelationshipEnd(table=t, columns=c) for (t, c, _) in children),
                orphan_policy=policy,  # type: ignore[arg-type]
                namespace=namespace,
            )
        )
    return tuple(out)


def _build_namespaces(config: dict[str, Any]) -> tuple[NamespaceBinding, ...]:
    """Read namespaces from config and produce NamespaceBinding tuples.

    S1 only consumes config-declared namespaces. S2 auto-binds FK child
    columns into their parent's namespace; that promotion lives in
    build_namespace_registry.
    """
    out: list[NamespaceBinding] = []
    namespaces = config.get("namespaces", {})
    if not isinstance(namespaces, dict):
        return tuple()
    for ns_name, ns_body in sorted(namespaces.items()):
        if not isinstance(ns_body, dict):
            continue
        declared_strings = ns_body.get("declared_by", []) or []
        declared_by: list[tuple[str, tuple[str, ...]]] = []
        for entry in declared_strings:
            if not isinstance(entry, str) or "." not in entry:
                continue
            table, col = entry.split(".", 1)
            declared_by.append((table, (col,)))
        # Seed material: stub derivation (job_seed XOR hash of ns_name).
        seed = _derive_namespace_seed(ns_name)
        out.append(
            NamespaceBinding(
                namespace=ns_name,
                declared_by=tuple(declared_by),
                seed=seed,
            )
        )
    return tuple(out)


def _build_seed_envelope_stub(config: dict[str, Any], profile: Profile) -> SeedEnvelope:
    """S1 stub seed-envelope derivation.

    Each column gets a placeholder ColumnSeed only if the config declares
    a strategy for it. Real HKDF-SHA256 derivation lands in S3
    (Determinism Layer); the enclosing Plan stamps
    `seed_protocol_version: 0` to flag this material as non-cryptographic.

    For S1, columns without a config-declared strategy stay out of the
    seed envelope entirely; the envelope is a structural slot the
    planner fills based on what the config actually masks.

    Composite relationships (M2 of the Dennis S1-finish review): every
    composite FK gets one GroupSeed on the CHILD table's per_group tuple,
    keyed by the canonical-joined column name (sorted column names joined
    with "__"). Composite-member columns on the child side are NOT
    emitted in per_column; the per_group entry covers them. Per S1 spec
    line 452 ("seed_envelope.per_group entries exist for every composite
    relationship; per_column entries do not duplicate composite-member
    columns").
    """
    job_seed_raw = config.get("global_settings", {}).get("seed", 0)
    try:
        job_seed = int(job_seed_raw)
    except (TypeError, ValueError):
        job_seed = 0

    # Index config table entries for fast lookup.
    config_tables_list = config.get("tables", [])
    config_tables: dict[str, dict[str, Any]] = {}
    if isinstance(config_tables_list, list):
        for t_entry in config_tables_list:
            if isinstance(t_entry, dict) and isinstance(t_entry.get("name"), str):
                config_tables[t_entry["name"]] = t_entry

    # M2 prep: identify composite relationships and the child columns that
    # are covered by a composite per_group entry. Those columns are
    # excluded from per_column on the child side; the per_group entry is
    # the authoritative seed material for the tuple.
    composite_child_cols: dict[str, set[str]] = {}
    composite_rels = [rel for rel in profile.relationships if len(rel.parent_columns) > 1]
    for rel in composite_rels:
        composite_child_cols.setdefault(rel.child_table, set()).update(rel.child_columns)

    per_table_out: list[tuple[str, TableSeed]] = []
    for table_profile in profile.tables:
        cfg_table = config_tables.get(table_profile.name)
        # Even if config has no entries for this table, we may still need
        # to emit per_group for composite relationships that target it.
        # Build per_column from config (if present) and per_group from
        # profile.relationships independently.
        table_seed = _derive_table_seed(job_seed, table_profile.name)
        composite_members_here = composite_child_cols.get(table_profile.name, set())

        per_column: list[tuple[str, ColumnSeed]] = []
        if cfg_table is not None:
            for col_entry in cfg_table.get("columns", []) or []:
                if not isinstance(col_entry, dict):
                    continue
                col_name = col_entry.get("name")
                # M2: skip composite-member columns; they're covered by per_group.
                if col_name in composite_members_here:
                    continue
                strategy = col_entry.get("strategy")
                provider = col_entry.get("provider")
                if not col_name or not strategy or not provider:
                    continue
                column_seed = _derive_column_seed(table_seed, col_name)
                backend_type_raw = col_entry.get("backend_type", "faker")
                backend_type = (
                    backend_type_raw
                    if backend_type_raw in ("faker", "mimesis", "pool", "decoy_native")
                    else "faker"
                )
                cardinality_mode_raw = col_entry.get("cardinality_mode", "reuse")
                cardinality_mode = (
                    cardinality_mode_raw
                    if cardinality_mode_raw
                    in (
                        "reuse",
                        "unique",
                        "match_source_cardinality",
                        "scale_source_cardinality",
                        "deterministic_map",
                    )
                    else "reuse"
                )
                provider_config_raw = col_entry.get("provider_config", {})
                if isinstance(provider_config_raw, dict):
                    provider_config = tuple(sorted(provider_config_raw.items()))
                else:
                    provider_config = tuple()
                coherent_with_raw = col_entry.get("coherent_with", []) or []
                coherent_with = tuple(c for c in coherent_with_raw if isinstance(c, str))
                per_column.append(
                    (
                        col_name,
                        ColumnSeed(
                            column_seed=column_seed,
                            namespace=col_entry.get("namespace"),
                            strategy=strategy,
                            provider=provider,
                            backend_type=backend_type,  # type: ignore[arg-type]
                            backend_version=col_entry.get("backend_version", "stub-0"),
                            cardinality_mode=cardinality_mode,  # type: ignore[arg-type]
                            provider_config=provider_config,
                            coherent_with=coherent_with,
                        ),
                    )
                )

        # M2: emit per_group entries for every composite relationship where
        # this table is the CHILD. Keyed by the canonical-joined column name
        # (sorted, "__"-joined). The GroupSeed.coherent_columns preserves
        # the declared child_columns order so S8 generators can match
        # source-tuple positions when composing the mask.
        per_group: list[tuple[str, GroupSeed]] = []
        for rel in composite_rels:
            if rel.child_table != table_profile.name:
                continue
            canonical_key = "__".join(sorted(rel.child_columns))
            group_seed = _derive_group_seed(table_seed, canonical_key)
            per_group.append(
                (
                    canonical_key,
                    GroupSeed(
                        group_seed=group_seed,
                        namespace=rel.namespace or "",
                        coherent_columns=rel.child_columns,
                    ),
                )
            )

        # Only emit a table entry if it has at least one per_column or per_group
        # entry; tables that contribute nothing stay out of the envelope.
        if not per_column and not per_group:
            continue
        per_table_out.append(
            (
                table_profile.name,
                TableSeed(
                    table_seed=table_seed,
                    per_column=tuple(per_column),
                    per_group=tuple(per_group),
                ),
            )
        )
    return SeedEnvelope(job_seed=job_seed, per_table=tuple(per_table_out))


# Stub seed derivation: not HKDF, not crypto. S3 replaces.


def _derive_namespace_seed(namespace: str) -> int:
    h = hashlib.sha256(f"ns::{namespace}".encode()).digest()
    return int.from_bytes(h[:8], "big")


def _derive_table_seed(job_seed: int, table_name: str) -> int:
    h = hashlib.sha256(f"tbl::{job_seed}::{table_name}".encode()).digest()
    return int.from_bytes(h[:8], "big")


def _derive_column_seed(table_seed: int, column_name: str) -> int:
    h = hashlib.sha256(f"col::{table_seed}::{column_name}".encode()).digest()
    return int.from_bytes(h[:8], "big")


def _derive_group_seed(table_seed: int, canonical_group_name: str) -> int:
    """Stub composite-group seed derivation (M2 of slice 4-6 review).

    Distinct prefix from `_derive_column_seed` so a group named the same
    as a column does not collide in the seed space. S3 replaces with
    real HKDF-SHA256 keyed material; this is non-cryptographic stub.
    """
    h = hashlib.sha256(f"grp::{table_seed}::{canonical_group_name}".encode()).digest()
    return int.from_bytes(h[:8], "big")
