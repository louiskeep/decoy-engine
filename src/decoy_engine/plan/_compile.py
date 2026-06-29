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
Every plan stamps `seed_protocol_version` from the determinism module's
constant (S3 stamped 1; the F-series corrections bumped it to 2).
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
# determinism module: S1 stamped 0 (placeholder); S3 stamped 1 (first
# real envelope per the v1 contract); the F-series corrections bumped to 2
# (coordinated Faker-seeding + canonicalize-integer fixes); QA walks/gen
# F3 (2026-06-01, PO Q-F3=b) bumped to 3 for the vectorised null-injection
# RNG-family swap (numpy.default_rng vs Python random.Random change the
# null pattern byte-for-byte); formula-hash migration (2026-06-01) bumped
# to 4: the formula sandbox `hash()` function swapped from legacy
# deterministic_hash to keyed HMAC-SHA256 via _formula_hash_keyed. Bumping
# requires a release-notes line per done-definition.md.
from decoy_engine.determinism import SEED_PROTOCOL_VERSION
from decoy_engine.plan._checks import (
    check_basic_uniqueness_pre_flight,
    check_composite_columns_length_match,
    check_derived_column_refs,
    check_fpe_checksum_scheme,
    check_non_poolable_provider_with_pool_backend,
    check_null_bearing_int_unsupported,
    check_statistical_columns,
    check_text_redact_ner_available,
    check_unknown_provider,
    check_vault_columns,
)

# SP-10b: derived_aggregate check extracted from _checks.py to keep that
# module under its allowlisted ceiling. See test_module_size.py ALLOWLIST.
from decoy_engine.plan._checks_derived_aggregate import check_derived_aggregate_refs

# SP-10c: per-strategy check modules for grouped_series, windowed_date, group_key.
from decoy_engine.plan._checks_group_key import check_group_key_refs
from decoy_engine.plan._checks_grouped_series import check_grouped_series_refs
from decoy_engine.plan._checks_windowed_date import check_windowed_date_refs
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._graph import _build_namespaces, _build_relationships

# Seed normalization lives in `plan/_seed.py` (single shared validator, F5).
from decoy_engine.plan._seed_envelope import _build_seed_envelope
from decoy_engine.plan._types import (
    OrderingNode,
    Plan,
    PlanCompileResult,
)
from decoy_engine.profile._hash import profile_hash
from decoy_engine.profile._types import Profile

PLAN_VERSION = 1


def compile_plan(
    config: dict[str, Any],
    profile: Profile,
    *,
    decoy_engine_version: str,
    no_profile: bool = False,
) -> Plan:
    """Compile (config, profile, engine_version) into a frozen Plan.

    Args:
        no_profile: when True, source distinct counts are treated as
            unavailable (S1's `--no-profile` mode, restored in S5 F4). The
            distinct-count-dependent checks (`basic_uniqueness_pre_flight`,
            `pool_capacity_pre_flight`) do not run and are recorded in
            `PlanCompileResult.checks_skipped` instead of `checks_passed`.
            Pool-backed UNIQUE columns still hard-error, because uniqueness
            cannot be guaranteed without distinct counts and cannot be
            deferred to runtime the way soft cardinality can. The structural
            checks (provider, composite-length, orphan policy, namespace, FK
            ordering) run regardless: they do not consume distinct counts.

    Raises:
        PlanCompileError: if any of the five S1 compile-time checks fails.
            The error carries `code`, `path`, and `message` for downstream
            UI rendering.
        PoolCapacityError: if a pool-backed column cannot be guaranteed
            enough capacity (see check_pool_capacity_pre_flight).

    Determinism contract: two calls with `__eq__`-equal inputs produce
    `__eq__`-equal Plans whose YAML serializations are byte-identical.
    """
    # Lazy import: see the module-level comment for cycle rationale.
    # Run the always-on checks. Each raises PlanCompileError on fail;
    # silence on pass means the check went into checks_passed.
    #
    # S2 wiring (per spec §4): namespace_ambiguity check moves into
    # build_namespace_registry; fk_plan_ordering check moves into
    # build_relationship_graph; orphan_fk_policy_completeness lands new at
    # row 6. The checks_passed tuple preserves S1's order plus the new
    # entry appended (the B1 regression contract: equals S1's list plus
    # exactly one new entry, in the documented position).
    # S5 wiring (per spec §6): pool_capacity_pre_flight (row 7) lives in
    # decoy_engine.generation.pool._validate. Lazy import same rationale
    # as the relationships block above.
    # S6 wiring (per spec §6): deterministic_namespace_completeness (row 9)
    # lives in decoy_engine.providers_v2.identifiers._validate. Lazy
    # import for symmetry with rows 6 + 7.
    from decoy_engine.generation.composite import composite_wiring_consistent
    from decoy_engine.generation.pool import check_pool_capacity_pre_flight
    from decoy_engine.providers_v2.identifiers import (
        deterministic_namespace_completeness,
    )
    from decoy_engine.relationships import (
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    namespace_registry = build_namespace_registry(config, profile)
    check_unknown_provider(config)
    # Row 11 (audit H5, 2026-06-12): structural (config + registry only),
    # runs in both branches right after unknown_provider so a missing
    # provider still surfaces as row 2 first.
    check_non_poolable_provider_with_pool_backend(config)
    # Row 12 (capability-gaps WS3, 2026-06-12): statistical generate
    # columns vs their snapshot artifacts. Config + artifact only, so it
    # runs in both branches and in run_config_only_checks.
    check_statistical_columns(config)
    # Row 13 (capability-gaps WS2, 2026-06-12): text_redact `ner` opt-in
    # requires the spacy extra + model on THIS host. Config + installed
    # packages only; both branches + run_config_only_checks.
    check_text_redact_ner_available(config)
    # Row 14 (deferred follow-up 1, 2026-06-12): vault: true needs a
    # namespace and a one-way strategy. Config-only; both branches +
    # run_config_only_checks.
    check_vault_columns(config)
    # Row 15 (SP-04 / P5.INFRA.1, 2026-06-27): reject unknown or structurally
    # unsupported FPE checksum schemes at compile time. Config-only; both branches +
    # run_config_only_checks.
    check_fpe_checksum_scheme(config)
    # Row 16 (SP-10 / P5.S.derived, 2026-06-28): reject derived columns whose
    # expression references a missing column or forms a cycle. Config-only; both
    # branches + run_config_only_checks.
    check_derived_column_refs(config)
    # Row 17 (SP-10b / P5.S.derived_aggregate, 2026-06-28): reject derived_aggregate
    # columns whose source column is missing or op is invalid. Config-only; both
    # branches + run_config_only_checks.
    check_derived_aggregate_refs(config)
    # Row 18 (SP-10c / P5.S.grouped_series.1, 2026-06-29): reject grouped_series
    # columns whose group_by or order_by column is missing. Config-only.
    check_grouped_series_refs(config)
    # Row 19 (SP-10c / P5.S.windowed_date, 2026-06-29): reject windowed_date
    # columns whose anchor column is missing. Config-only.
    check_windowed_date_refs(config)
    # Row 20 (SP-10c / P5.P.group_key, 2026-06-29): reject group_key columns
    # whose group_by column is missing. Config-only.
    check_group_key_refs(config)
    check_composite_columns_length_match(profile)
    # MG-3 / M3 (2026-05-31): reject when + coherent_with combo early,
    # before composite-wiring checks. A column carrying both fields is
    # ill-defined (composite generators write the bundle, not the
    # column), so the operator should see the typed when_with_coherent_
    # with_unsupported error rather than a composite-wiring follow-on.
    _check_when_with_coherent_with(config)
    # Row 8 (S8): composite wiring. Structural (config + registry), so it runs
    # in both --no-profile and full modes, like row 9.
    composite_wiring_consistent(config, namespace_registry)
    orphan_policy_lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
    relationship_graph = build_relationship_graph(
        profile.relationships,
        namespace_registry=namespace_registry,
        orphan_policy_lookup=orphan_policy_lookup,
    )
    # Row 7 (S5): pool-backed columns supersede the S1 unique check.
    # on_pool_exhaustion default is 'scale_up' (PO PQ3). UNIQUE columns
    # hard-error regardless of this setting (F3); soft modes defer under
    # scale_up/fall_back and raise under 'fail'.
    on_pool_exhaustion = config.get("global_settings", {}).get("on_pool_exhaustion", "scale_up")
    # F4 (--no-profile): the two distinct-count-dependent checks
    # (basic_uniqueness_pre_flight + pool_capacity_pre_flight) cannot verify
    # capacity without source distinct counts. They are recorded in
    # checks_skipped rather than checks_passed. pool_capacity still runs (it
    # hard-errors on UNIQUE columns, which cannot be deferred); its soft-mode
    # verification is what's skipped.
    # Row 9 (S6 + S7): deterministic_namespace_completeness is structural (no
    # distinct counts), so it runs and lands in checks_passed in both branches.
    if no_profile:
        capacity_warnings = check_pool_capacity_pre_flight(
            config, profile, on_pool_exhaustion=on_pool_exhaustion, no_profile=True
        )
        deterministic_namespace_completeness(config)
        checks_passed: tuple[str, ...] = (
            "namespace_ambiguity",
            "unknown_provider",
            "fk_plan_ordering",
            "composite_columns_length_match",
            "orphan_fk_policy_completeness",
            "composite_wiring_consistent",
            "deterministic_namespace_completeness",
            # Row 11 (audit H5): structural, tail-appended.
            "non_poolable_provider_with_pool_backend",
            # Row 12 (WS3): structural, tail-appended.
            "statistical_columns",
            # Row 13 (WS2): structural, tail-appended.
            "text_redact_ner_available",
            # Row 14 (vault): structural, tail-appended.
            "vault_columns",
            # Row 15 (SP-04): structural, tail-appended.
            "fpe_checksum_scheme",
            # Row 16 (SP-10): derived column-ref / cycle check, tail-appended.
            "derived_column_refs",
            # Row 17 (SP-10b): derived_aggregate source-column + op check.
            "derived_aggregate_refs",
            # Row 18 (SP-10c): grouped_series group_by + order_by column refs.
            "grouped_series_refs",
            # Row 19 (SP-10c): windowed_date anchor column ref.
            "windowed_date_refs",
            # Row 20 (SP-10c): group_key group_by column ref.
            "group_key_refs",
        )
        checks_skipped: tuple[str, ...] = (
            "basic_uniqueness_pre_flight",
            "pool_capacity_pre_flight",
            # Row 10 (B1, S13): profile-dependent, so skipped here; the
            # execution-time guard rejects the same input on both adapters.
            "null_bearing_int_unsupported",
        )
    else:
        check_basic_uniqueness_pre_flight(config, profile)
        capacity_warnings = check_pool_capacity_pre_flight(
            config, profile, on_pool_exhaustion=on_pool_exhaustion
        )
        deterministic_namespace_completeness(config)
        # Row 10 (B1, S13): reject integer + null-bearing columns under
        # truncate/hash/categorical. Profile-dependent (dtype + null_count), so it
        # runs here and is skipped under no_profile (the execution-time guard backs
        # it up there).
        check_null_bearing_int_unsupported(config, profile)
        checks_passed = (
            "namespace_ambiguity",
            "unknown_provider",
            "fk_plan_ordering",
            "basic_uniqueness_pre_flight",
            "composite_columns_length_match",
            "orphan_fk_policy_completeness",
            "pool_capacity_pre_flight",
            "composite_wiring_consistent",
            "deterministic_namespace_completeness",
            "null_bearing_int_unsupported",
            # Row 11 (audit H5): structural, tail-appended.
            "non_poolable_provider_with_pool_backend",
            # Row 12 (WS3): structural, tail-appended.
            "statistical_columns",
            # Row 13 (WS2): structural, tail-appended.
            "text_redact_ner_available",
            # Row 14 (vault): structural, tail-appended.
            "vault_columns",
            # Row 15 (SP-04): structural, tail-appended.
            "fpe_checksum_scheme",
            # Row 16 (SP-10): derived column-ref / cycle check, tail-appended.
            "derived_column_refs",
            # Row 17 (SP-10b): derived_aggregate source-column + op check.
            "derived_aggregate_refs",
            # Row 18 (SP-10c): grouped_series group_by + order_by column refs.
            "grouped_series_refs",
            # Row 19 (SP-10c): windowed_date anchor column ref.
            "windowed_date_refs",
            # Row 20 (SP-10c): group_key group_by column ref.
            "group_key_refs",
        )
        checks_skipped = ()

    # Hashes.
    cfg_hash = _hash_config(config)
    prof_hash = profile_hash(profile)

    # Build the constituent blocks. Relationship + ordering blocks derive
    # from the relationship_graph (S2 §4 wiring); namespaces still build
    # from config because the YAML shape carries seed material the
    # registry doesn't yet track (S3 promotes this).
    relationships = _build_relationships(config, profile, orphan_policy_lookup=orphan_policy_lookup)
    namespaces = _build_namespaces(config)
    ordering = tuple(OrderingNode(table=t, columns=c) for (t, c) in relationship_graph.ordering)
    seed_envelope, stamp_warnings = _build_seed_envelope(config, profile)

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
        plan_compile=PlanCompileResult(
            checks_passed=checks_passed,
            checks_skipped=checks_skipped,
            warnings=stamp_warnings + capacity_warnings,
        ),
    )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def run_config_only_checks(config: dict[str, Any]) -> tuple[str, ...]:
    """Run the profile-free subset of plan-compile checks.

    Audit H5 (2026-06-12): `decoy validate` only schema-checked configs,
    so configs guaranteed to crash at `run` (e.g. `strategy: faker` on a
    non-poolable provider) validated green. This is the strict subset of
    `compile_plan`'s checks that consume only the config dict + the
    provider registry -- no Profile, no source I/O -- so a config-only
    caller can reject exactly what `run` would reject without false
    positives (naive `compile_plan(no_profile=True)` is NOT equivalent:
    it hard-errors on `cardinality_mode: unique` columns that a profiled
    run accepts).

    Profile-dependent checks (basic_uniqueness / pool_capacity /
    null_bearing_int / namespace + FK graph / composite wiring) stay
    compile-time-only.

    Raises:
        PlanCompileError: on the first failing check.

    Returns:
        Names of the checks that ran (for caller reporting).
    """
    from decoy_engine.providers_v2.identifiers import (
        deterministic_namespace_completeness,
    )

    check_unknown_provider(config)
    _check_when_with_coherent_with(config)
    deterministic_namespace_completeness(config)
    check_non_poolable_provider_with_pool_backend(config)
    # Row 12 (WS3): consumes the config plus its referenced snapshot
    # artifact (a fitted-model JSON, not source data), so config-only
    # callers catch a missing/incompatible artifact before a long run.
    check_statistical_columns(config)
    # Row 13 (WS2): text_redact `ner` opt-in needs spacy + model here.
    check_text_redact_ner_available(config)
    # Row 14 (vault): vault: true needs a namespace + a one-way strategy.
    check_vault_columns(config)
    # Row 15 (SP-04): reject unknown or structurally unsupported FPE checksum schemes.
    check_fpe_checksum_scheme(config)
    # Row 16 (SP-10 / P5.S.derived): reject derived expression with missing or
    # cyclic column refs. Config-only; config + Lark parse only.
    check_derived_column_refs(config)
    # Row 17 (SP-10b / P5.S.derived_aggregate): reject derived_aggregate source
    # column refs that are missing or have an invalid op. Config-only.
    check_derived_aggregate_refs(config)
    # Row 18 (SP-10c / P5.S.grouped_series.1): reject grouped_series columns with
    # missing group_by or order_by column refs. Config-only.
    check_grouped_series_refs(config)
    # Row 19 (SP-10c / P5.S.windowed_date): reject windowed_date columns with a
    # missing anchor column ref. Config-only.
    check_windowed_date_refs(config)
    # Row 20 (SP-10c / P5.P.group_key): reject group_key columns with a missing
    # group_by column ref. Config-only.
    check_group_key_refs(config)
    return (
        "unknown_provider",
        "when_with_coherent_with",
        "deterministic_namespace_completeness",
        "non_poolable_provider_with_pool_backend",
        "statistical_columns",
        "text_redact_ner_available",
        "vault_columns",
        "fpe_checksum_scheme",
        "derived_column_refs",
        "derived_aggregate_refs",
        "grouped_series_refs",
        "windowed_date_refs",
        "group_key_refs",
    )


def _check_when_with_coherent_with(config: dict[str, Any]) -> None:
    """MG-3 / M3 (2026-05-31): reject `when` + `coherent_with` combo
    at compile time with a typed error code.

    The composite generator writes the bundle, not the column. A
    per-column row gate on a coherent_with column is ill-defined:
    skipping the row on one column but not its siblings would
    desynchronize the bundle. The operator sees the typed error and
    can either drop `when` or move the column off the coherent set.
    """
    tables = config.get("tables", []) or []
    for table in tables:
        table_name = table.get("name", "?") if isinstance(table, dict) else "?"
        columns = (table or {}).get("columns", []) if isinstance(table, dict) else []
        for col in columns or []:
            if not isinstance(col, dict):
                continue
            col_name = col.get("name", "?")
            when = col.get("when")
            coherent_with = col.get("coherent_with") or []
            if (
                isinstance(when, str)
                and when.strip()
                and isinstance(coherent_with, (list, tuple))
                and len(coherent_with) > 0
            ):
                raise PlanCompileError(
                    code="when_with_coherent_with_unsupported",
                    path=f"tables.{table_name}.columns.{col_name}.when",
                    message=(
                        f"Column {table_name}.{col_name}: `when:` is not "
                        "supported on columns participating in "
                        "`coherent_with`; the composite generator writes "
                        "the bundle, not the column. Drop `when:` here or "
                        "move the column off the coherent set."
                    ),
                )


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
    # QA walks/generators F9 (2026-06-01, LOW correctness): no
    # default=str. Pre-fix json.dumps silently called str() on any
    # non-JSON-native value (datetime, UUID, dataclass), so two
    # semantically different values that str-format identically
    # produced the same hash. In practice configs arrive via
    # yaml.safe_load (JSON-native only) so the bug never triggered,
    # but the silent coercion would have hidden any future code path
    # that fed non-native types into the planner. Now any such value
    # raises TypeError at plan-compile time.
    canonical = json.dumps(
        semantic_config,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
