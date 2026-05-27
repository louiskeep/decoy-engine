"""compile_plan: the keystone S1 deliverable.

`compile_plan(config, profile, *, decoy_engine_version)` consumes a
parsed pipeline config + a Profile + the engine version stamp and
produces a frozen Plan. Pure function: same inputs -> byte-identical
output. Validation runs always (never flag-gated); failures raise
`PlanCompileError` with `code` + `path` + `message`.

S1 shipped five foundational checks (compile-check ownership table
rows 1-5). S2 promoted relationship + namespace into
`decoy_engine.relationships` (the namespace_ambiguity + fk_plan_ordering
checks moved out of this module into the registry + graph builders)
and added `orphan_fk_policy_completeness` at row 6. S2-S9 follow this
relocate-or-add pattern; the check-runner here is the slot they slot into.

S3 replaced S1's stub seed envelope with the determinism layer's keyed
material per the spec §5.5 plan-schema delta: `SeedEnvelope.job_seed`
is now `bytes` (the sole entropy input to
`decoy_engine.determinism.derive(...)`); the per-context `_seed` int
fields are gone; the four `_derive_*_seed` stub helpers were deleted.
Every plan stamps `seed_protocol_version: 1` from the determinism
module's constant.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Note: imports from decoy_engine.relationships are deferred inside
# compile_plan to break a circular import. decoy_engine/__init__.py
# eagerly loads decoy_engine.relationships (per S2 spec API summary),
# which transitively triggers loading plan._errors -> plan/__init__ ->
# this module -> relationships (partially init). Lazy import inside the
# function body cuts the cycle without changing the call surface.
# S1's plan_version is 1. SEED_PROTOCOL_VERSION imported from the
# determinism module: S1 stamped 0 (placeholder); S3 stamps 1 (first
# real envelope per the v1 contract). Bumping in future requires a
# release-notes line per done-definition.md.
from decoy_engine.determinism import SEED_PROTOCOL_VERSION
from decoy_engine.plan._checks import (
    check_basic_uniqueness_pre_flight,
    check_composite_columns_length_match,
    check_unknown_provider,
)
from decoy_engine.plan._errors import PlanCompileError
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

PLAN_VERSION = 1


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
    # Lazy import: see the module-level comment for cycle rationale.
    from decoy_engine.relationships import (
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    # Run the always-on checks. Each raises PlanCompileError on fail;
    # silence on pass means the check went into checks_passed.
    #
    # S2 wiring (per spec §4): namespace_ambiguity check moves into
    # build_namespace_registry; fk_plan_ordering check moves into
    # build_relationship_graph; orphan_fk_policy_completeness lands new at
    # row 6. The checks_passed tuple preserves S1's order plus the new
    # entry appended (the B1 regression contract: equals S1's list plus
    # exactly one new entry, in the documented position).
    namespace_registry = build_namespace_registry(config, profile)
    check_unknown_provider(config)
    check_composite_columns_length_match(profile)
    orphan_policy_lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
    relationship_graph = build_relationship_graph(
        profile.relationships,
        namespace_registry=namespace_registry,
        orphan_policy_lookup=orphan_policy_lookup,
    )
    check_basic_uniqueness_pre_flight(config, profile)

    checks_passed = (
        "namespace_ambiguity",
        "unknown_provider",
        "fk_plan_ordering",
        "basic_uniqueness_pre_flight",
        "composite_columns_length_match",
        "orphan_fk_policy_completeness",
    )

    # Hashes.
    cfg_hash = _hash_config(config)
    prof_hash = profile_hash(profile)

    # Build the constituent blocks. Relationship + ordering blocks derive
    # from the relationship_graph (S2 §4 wiring); namespaces still build
    # from config because the YAML shape carries seed material the
    # registry doesn't yet track (S3 promotes this).
    relationships = _build_relationships(config, profile)
    namespaces = _build_namespaces(config)
    ordering = tuple(OrderingNode(table=t, columns=c) for (t, c) in relationship_graph.ordering)
    seed_envelope = _build_seed_envelope(config, profile)

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
    """SHA-256 over a canonical JSON serialization of the masking-semantics
    portion of the config (M1 from S1 end-of-sprint Dennis review).

    The `sources` and `targets` blocks are explicitly excluded: they
    describe data binding (where bytes come from, where bytes go) rather
    than masking semantics. A user swapping a local file source for an
    S3 source does not change which columns mask how; the
    pipeline_config_hash must stay byte-identical across that swap so
    audit + reproducibility tooling can match the two runs as
    semantically equivalent.

    Sort_keys=True, ensure_ascii=True, separators=(",", ":") for byte
    stability across Python runtimes. Same masking semantics produce the
    same hash regardless of key insertion order or source/target binding.
    """
    semantic_config = {k: v for k, v in config.items() if k not in ("sources", "targets")}
    canonical = json.dumps(
        semantic_config,
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
        out.append(
            NamespaceBinding(
                namespace=ns_name,
                declared_by=tuple(declared_by),
            )
        )
    return tuple(out)


def _normalize_job_seed(config: dict[str, Any]) -> bytes:
    """Normalize the config-side `seed` value to the 8-byte bytes form
    that `decoy_engine.determinism.derive(...)` consumes.

    Per S3 spec §5.5 (resolution of B2 + H1): the int -> bytes conversion
    happens exactly once at the pipeline-config adapter boundary. The
    rest of the engine consumes `bytes` only.

    Raises:
        PlanCompileError(code='seed_overflow') if the int does not fit
        in unsigned 64-bit (the size of the bytes form).
    """
    job_seed_raw = config.get("global_settings", {}).get("seed", 0)
    try:
        seed_int = int(job_seed_raw)
    except (TypeError, ValueError):
        seed_int = 0
    if not 0 <= seed_int < (1 << 64):
        raise PlanCompileError(
            code="seed_overflow",
            path="global_settings.seed",
            message=(f"seed must fit in unsigned 64-bit (range [0, 2**64)); got {seed_int}"),
        )
    return seed_int.to_bytes(8, "big")


def _build_seed_envelope(config: dict[str, Any], profile: Profile) -> SeedEnvelope:
    """Construct the SeedEnvelope from config + profile.

    Per S3 spec §5.5 plan-schema delta: no per-column / per-table / per-group
    seed integers. `derive(plan.seed_envelope.job_seed, namespace, source_bytes)`
    is the source of truth for stable bytes; per-context distinctness comes
    from the namespace string + source bytes, not from a per-context seed.

    For columns without a config-declared strategy, the planner stays out of
    the seed envelope entirely; the envelope is a structural slot the planner
    fills based on what the config actually masks.

    Composite relationships (M2 from the S1 finish review, preserved here):
    every composite FK gets one GroupSeed on the CHILD table's per_group
    tuple, keyed by the canonical-joined column name (sorted child columns
    joined with "__"). Composite-member columns on the child side are NOT
    emitted in per_column; the per_group entry covers them.
    """
    job_seed = _normalize_job_seed(config)

    # Index config table entries for fast lookup.
    config_tables_list = config.get("tables", [])
    config_tables: dict[str, dict[str, Any]] = {}
    if isinstance(config_tables_list, list):
        for t_entry in config_tables_list:
            if isinstance(t_entry, dict) and isinstance(t_entry.get("name"), str):
                config_tables[t_entry["name"]] = t_entry

    composite_child_cols: dict[str, set[str]] = {}
    composite_rels = [rel for rel in profile.relationships if len(rel.parent_columns) > 1]
    for rel in composite_rels:
        composite_child_cols.setdefault(rel.child_table, set()).update(rel.child_columns)

    per_table_out: list[tuple[str, TableSeed]] = []
    for table_profile in profile.tables:
        cfg_table = config_tables.get(table_profile.name)
        composite_members_here = composite_child_cols.get(table_profile.name, set())

        per_column: list[tuple[str, ColumnSeed]] = []
        if cfg_table is not None:
            for col_entry in cfg_table.get("columns", []) or []:
                if not isinstance(col_entry, dict):
                    continue
                col_name = col_entry.get("name")
                if col_name in composite_members_here:
                    continue
                strategy = col_entry.get("strategy")
                provider = col_entry.get("provider")
                if not col_name or not strategy or not provider:
                    continue
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

        per_group: list[tuple[str, GroupSeed]] = []
        for rel in composite_rels:
            if rel.child_table != table_profile.name:
                continue
            canonical_key = "__".join(sorted(rel.child_columns))
            per_group.append(
                (
                    canonical_key,
                    GroupSeed(
                        namespace=rel.namespace or "",
                        coherent_columns=rel.child_columns,
                    ),
                )
            )

        if not per_column and not per_group:
            continue
        per_table_out.append(
            (
                table_profile.name,
                TableSeed(
                    per_column=tuple(per_column),
                    per_group=tuple(per_group),
                ),
            )
        )
    return SeedEnvelope(job_seed=job_seed, per_table=tuple(per_table_out))
