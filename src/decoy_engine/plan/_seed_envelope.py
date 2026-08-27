"""SeedEnvelope construction, split from _compile.py (F11d).

Builds the per-table / per-column / per-group SeedEnvelope from config +
profile + the provider registry. The providers_v2 and storm.ner imports stay
deferred inside the function body (real runtime cycle; see F12). Imported back
into _compile.py; SEED_PROTOCOL_VERSION and the persisted format are untouched.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.execution._distribution_behavior import distribution_behavior_for
from decoy_engine.execution._technique_class import technique_class_for
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._pool_size import resolve_pool_size
from decoy_engine.plan._seed import _normalize_job_seed
from decoy_engine.plan._types import (
    ColumnSeed,
    GroupSeed,
    SeedEnvelope,
    TableSeed,
)
from decoy_engine.profile._types import Profile, Relationship


def composite_fk_relationships(profile: Profile) -> list[Relationship]:
    """Relationships whose FK is composite (a multi-column parent key).

    The single source of the composite-FK-group predicate: a composite FK
    collapses its child columns into one ``GroupSeed`` on the child table's
    ``per_group`` (so ``build_work_list`` emits a ``composite_fk_group`` node
    for it, not per-column scalar nodes). The native planning boundary reads
    this same helper so ``native_route_eligibility`` and ``compile_native_plan``
    classify FK-group nodes identically, instead of re-inlining the rule.
    """
    return [rel for rel in profile.relationships if len(rel.parent_columns) > 1]


def _build_seed_envelope(
    config: dict[str, Any], profile: Profile
) -> tuple[SeedEnvelope, tuple[str, ...]]:
    """Construct the SeedEnvelope from config + profile + the registry.

    Returns `(envelope, warnings)`: the warnings tuple carries any
    `backend_stamp_user_override_ignored` entries from H1 (the user
    declared `backend_type:` / `backend_version:` that differs from
    the registry's binding; the registry wins, the user value is
    ignored, and the planner emits a non-blocking warning).

    Per S3 spec §5.5 plan-schema delta: no per-column / per-table / per-group
    seed integers. `derive(plan.seed_envelope.job_seed, namespace, source_bytes)`
    is the source of truth for stable bytes.

    Per S4 spec §9 (H1 PO call): the planner consults
    `get_default_registry().get_capabilities(provider)` for the column's
    `backend_type` + `backend_version`. The user-supplied YAML fields
    are IGNORED for the stamp (registry is source of truth). If the
    user supplied a contradicting value, a warning lands in the returned
    tuple; the warning does not block compile, and the stamp uses the
    registry value. The default `backend_version: "stub-0"` from S1 is
    removed.

    Composite relationships (M2 from the S1 finish review, preserved here):
    every composite FK gets one GroupSeed on the CHILD table's per_group
    tuple, keyed by the canonical-joined column name (sorted child columns
    joined with "__"). Composite-member columns on the child side are NOT
    emitted in per_column; the per_group entry covers them.
    """
    # Deferred import: see module-level cycle comment.
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.providers_v2._errors import ProviderError as _ProviderError

    registry = get_default_registry()
    warnings: list[str] = []
    job_seed = _normalize_job_seed(config)

    # Index config table entries for fast lookup.
    config_tables_list = config.get("tables", [])
    config_tables: dict[str, dict[str, Any]] = {}
    if isinstance(config_tables_list, list):
        for t_entry in config_tables_list:
            if isinstance(t_entry, dict) and isinstance(t_entry.get("name"), str):
                config_tables[t_entry["name"]] = t_entry

    composite_child_cols: dict[str, set[str]] = {}
    composite_rels = composite_fk_relationships(profile)
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
                # D4: a generator strategy (faker) needs a provider to produce values;
                # scalar transforms (hash/redact/truncate/bucketize/date_shift/fpe/
                # categorical/shuffle/passthrough) have no provider and read their
                # settings from provider_config. Drop a column only if it lacks a
                # name/strategy, or is a faker column with no provider (it cannot
                # generate). A provider-less scalar column now correctly produces a work
                # node and gets masked (previously it was silently dropped -> unmasked).
                if not col_name or not strategy:
                    continue
                if strategy == "faker" and not provider:
                    continue
                # H1: consult registry for backend_type + backend_version (faker only;
                # scalar columns have no provider, so reg_caps stays None -> fallback
                # stamp). User-supplied YAML fields are ignored for the stamp; if they
                # contradict the registry, emit a warning.
                reg_caps = None
                if provider:
                    try:
                        reg_caps = registry.get_capabilities(provider)
                    except _ProviderError:
                        # check_unknown_provider should have caught this earlier
                        # in compile_plan; defensively fall back to the legacy
                        # behavior here so a bug in the check-runner doesn't
                        # crash the planner.
                        reg_caps = None
                if reg_caps is not None:
                    backend_type = reg_caps.backend_type
                    backend_version = reg_caps.backend_version
                    user_backend_type = col_entry.get("backend_type")
                    user_backend_version = col_entry.get("backend_version")
                    if user_backend_type is not None and user_backend_type != backend_type:
                        warnings.append(
                            f"backend_stamp_user_override_ignored: column "
                            f"{table_profile.name}.{col_name} declared "
                            f"backend_type={user_backend_type!r}; registry "
                            f"binds {provider!r} to {backend_type!r} "
                            "(registry wins per S4 §9)."
                        )
                    if user_backend_version is not None and user_backend_version != backend_version:
                        warnings.append(
                            f"backend_stamp_user_override_ignored: column "
                            f"{table_profile.name}.{col_name} declared "
                            f"backend_version={user_backend_version!r}; registry "
                            f"binds {provider!r} to {backend_version!r} "
                            "(registry wins per S4 §9)."
                        )
                else:
                    backend_type_raw = col_entry.get("backend_type", "faker")
                    backend_type = (
                        backend_type_raw
                        if backend_type_raw in ("faker", "mimesis", "pool", "decoy_native")
                        else "faker"
                    )
                    backend_version = col_entry.get("backend_version", "stub-0")
                cardinality_mode_raw = col_entry.get("cardinality_mode", "reuse")
                # R6 reshape (S5): `deterministic_map` is deleted from the
                # enum; rename error directs to the new shape.
                if cardinality_mode_raw == "deterministic_map":
                    raise PlanCompileError(
                        code="plan_schema_deterministic_map_renamed",
                        path=(f"tables.{table_profile.name}.columns.{col_name}.cardinality_mode"),
                        message=(
                            f"Column {table_profile.name}.{col_name}: "
                            "`cardinality_mode: deterministic_map` is no longer "
                            "a valid value after the R6 reshape (S5). The "
                            "deterministic-vs-random axis is now a separate "
                            "first-class field. Migrate to:\n"
                            "    deterministic: true\n"
                            "    cardinality_mode: reuse   # or another mode\n"
                            "See S5 spec §6 + cross-sprint contracts R6."
                        ),
                    )
                cardinality_mode = (
                    cardinality_mode_raw
                    if cardinality_mode_raw
                    in (
                        "reuse",
                        "unique",
                        "match_source_cardinality",
                        "scale_source_cardinality",
                    )
                    else "reuse"
                )
                # R6: read the new first-class `deterministic: bool` field.
                # Defaults to False; the column opts in explicitly.
                deterministic = bool(col_entry.get("deterministic", False))
                # Gap-closure item 2: `allow_collisions: true` is a documented
                # alias for Delphix Secure Lookup's collision-allowed semantics.
                # It compiles to reuse + deterministic (a stable many-to-one
                # masked map). Conflicts with any non-`reuse` cardinality_mode;
                # the namespace requirement is enforced by the row-9
                # deterministic_namespace_completeness pre-check.
                if bool(col_entry.get("allow_collisions", False)):
                    explicit_mode = col_entry.get("cardinality_mode")
                    if explicit_mode is not None and explicit_mode != "reuse":
                        raise PlanCompileError(
                            code="allow_collisions_mode_conflict",
                            path=(
                                f"tables.{table_profile.name}.columns.{col_name}.allow_collisions"
                            ),
                            message=(
                                f"Column {table_profile.name}.{col_name}: "
                                "`allow_collisions: true` forces `cardinality_mode: "
                                f"reuse`, which conflicts with the declared "
                                f"`cardinality_mode: {explicit_mode}`. Drop one: "
                                "use allow_collisions for intentional collisions, "
                                "or the explicit mode for distinct output."
                            ),
                        )
                    cardinality_mode = "reuse"
                    deterministic = True
                provider_config_raw = col_entry.get("provider_config", {})
                if isinstance(provider_config_raw, dict):
                    provider_config = tuple(sorted(provider_config_raw.items()))
                else:
                    provider_config_raw = {}
                    provider_config = tuple()
                # pool_size is resolved once here so runtime consumers read a
                # single typed source of truth; top-level wins, provider_config
                # is the fallback, contradictions fail closed. The compile-time
                # UNIQUE-capacity preflight (plan/_checks.py) resolves through
                # the same `resolve_pool_size` helper so the two readers cannot
                # disagree about a provider_config-only declaration.
                resolved_pool_size = resolve_pool_size(
                    col_entry, table_name=table_profile.name, col_name=col_name
                )
                # `scale` has one documented location today (top-level
                # ColumnConfig.scale; config/_tables.py); no runtime or
                # compile-time reader consults provider_config.scale, so
                # there is nothing to reconcile it against.
                scale_raw = col_entry.get("scale")
                resolved_scale = float(scale_raw) if scale_raw is not None else None
                coherent_with_raw = col_entry.get("coherent_with", []) or []
                coherent_with = tuple(c for c in coherent_with_raw if isinstance(c, str))
                # MG-3 / M3 (2026-05-31): optional per-row gate
                # expression. Reject when: combined with coherent_with
                # at compile time -- composite generators write the
                # bundle, not the column, so per-column row gating is
                # ill-defined for the coherent set (spec §Pitfalls).
                when_raw = col_entry.get("when")
                when = when_raw if isinstance(when_raw, str) and when_raw.strip() else None
                # Deferred follow-up 8c: stamp the installed NER model
                # version for text_redact + ner columns (precedent: the
                # backend_version registry stamp above). Row 13 already
                # hard-fails a truly-missing model; a None version here
                # means the package ships no metadata, which only weakens
                # the cross-environment audit trail, so warn instead.
                # TX-2 (2026-07-20): text_mask's `ner` opt-in needs the SAME
                # stamp + drift guard as text_redact -- without it, a model
                # upgrade between compile and run would silently change which
                # spans mask_cell synthesizes for the same config + seed.
                ner_model_version = None
                if strategy in ("text_redact", "text_mask") and isinstance(
                    provider_config_raw, dict
                ):
                    ner_cfg = provider_config_raw.get("ner")
                    if ner_cfg:
                        from decoy_engine.storm.ner import (
                            DEFAULT_NER_MODEL,
                            installed_model_version,
                        )

                        ner_model = DEFAULT_NER_MODEL
                        if isinstance(ner_cfg, dict) and ner_cfg.get("model"):
                            ner_model = str(ner_cfg["model"])
                        ner_model_version = installed_model_version(ner_model)
                        if ner_model_version is None:
                            warnings.append(
                                f"ner_model_version_unavailable: column "
                                f"{table_profile.name}.{col_name} uses ner model "
                                f"{ner_model!r} which has no installed package "
                                f"metadata; {strategy} output is byte-stable only "
                                "within this environment."
                            )
                if when is not None and coherent_with:
                    raise PlanCompileError(
                        code="when_with_coherent_with_unsupported",
                        path=f"tables.{table_profile.name}.columns.{col_name}.when",
                        message=(
                            f"Column {table_profile.name}.{col_name}: `when:` is "
                            "not supported on columns participating in "
                            "`coherent_with`; the composite generator writes the "
                            "bundle, not the column. Drop `when:` here or move "
                            "the column off the coherent set."
                        ),
                    )
                per_column.append(
                    (
                        col_name,
                        ColumnSeed(
                            namespace=col_entry.get("namespace"),
                            strategy=strategy,
                            provider=provider,
                            backend_type=backend_type,  # type: ignore[arg-type]
                            backend_version=backend_version,
                            cardinality_mode=cardinality_mode,  # type: ignore[arg-type]
                            deterministic=deterministic,
                            provider_config=provider_config,
                            coherent_with=coherent_with,
                            # MG-1 S1 (2026-06-01): GDPR technique class from
                            # the central registry; None when the strategy
                            # has not been classified.
                            technique_class=technique_class_for(strategy),
                            when=when,
                            # MG-6 D1 (2026-05-31): distribution-behavior
                            # classification. Resolves dynamically for the
                            # categorical case (preserves_all when weights/
                            # from_profile set, destroys_frequency otherwise).
                            # nested resolves to "inherits"; the manifest
                            # layer substitutes the child's behavior.
                            distribution_behavior=distribution_behavior_for(
                                strategy, provider_config
                            ),
                            ner_model_version=ner_model_version,
                            pool_size=resolved_pool_size,
                            scale=resolved_scale,
                            # DE-02 (6b): a vault:true column persists a reversible
                            # source->masked mapping, so it is keyed surface that
                            # the GA gate must require a secret for.
                            vault=bool(col_entry.get("vault", False)),
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
    envelope = SeedEnvelope(job_seed=job_seed, per_table=tuple(per_table_out))
    return envelope, tuple(warnings)
