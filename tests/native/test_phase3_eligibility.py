"""Task 3.3: `phase3_c1_eligibility`, the config-aware Phase 3 admission query.

Covers the JC-5 precondition split into distinct coded reasons (Task 3.1's
`faker_pool_precondition_met` collapses the same checks into one bool; this
predicate names each one), the C1 provider allowlist, the config shapes the
native route cannot honor (`vault: true`, `when:`), the `allow_collisions`
compile-time coercion and its mode conflict, duplicate-declaration safety,
totality over the live provider registry, and the layering cross-check against
`native_route_eligibility`.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.execution.native._phase3_eligibility import (
    C1_PROVIDER_ALLOWLIST,
    phase3_c1_eligibility,
)
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.profile import ColumnProfile, Profile, TableProfile
from decoy_engine.providers_v2 import get_default_registry


def _config(table: str, *columns: dict) -> dict:
    return {"tables": [{"name": table, "columns": list(columns)}]}


def _faker_col(
    name: str = "FIRST",
    *,
    provider: str = "person_first_name",
    deterministic: bool | None = True,
    namespace: str | None = "ns_first",
    pool_size: int | None = 10_000,
    cardinality_mode: str | None = None,
    vault: bool = False,
    when: str | None = None,
    allow_collisions: bool = False,
) -> dict:
    col: dict = {"name": name, "strategy": "faker", "provider": provider}
    if deterministic is not None:
        col["deterministic"] = deterministic
    if namespace is not None:
        col["namespace"] = namespace
    if pool_size is not None:
        col["pool_size"] = pool_size
    if cardinality_mode is not None:
        col["cardinality_mode"] = cardinality_mode
    if vault:
        col["vault"] = True
    if when is not None:
        col["when"] = when
    if allow_collisions:
        col["allow_collisions"] = True
    return col


def _hash_col(name: str, namespace: str) -> dict:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _rejection_codes(result) -> set[str]:
    return {r.split(":", 1)[0] for r in result.reasons}


# ---------------------------------------------------------------------------
# The frozen deterministic C1 variant admits, both tables, no rejection.
# ---------------------------------------------------------------------------


def test_frozen_c1_patients_table_admits() -> None:
    # docs/plans/PHASE3-C1-BASELINE.md's frozen recipe: FIRST/LAST/MAIDEN
    # deterministic-reuse faker + six hash columns, no relationships.
    config = _config(
        "patients",
        _faker_col("FIRST", provider="person_first_name", namespace="first_name_identity"),
        _faker_col("LAST", provider="person_last_name", namespace="last_name_identity"),
        _faker_col("MAIDEN", provider="person_last_name", namespace="maiden_name_identity"),
        _hash_col("SSN", "ssn_identity"),
        _hash_col("DRIVERS", "drivers_identity"),
        _hash_col("PASSPORT", "passport_identity"),
        _hash_col("ADDRESS", "address_identity"),
        _hash_col("BIRTHDATE", "birthdate_identity"),
        _hash_col("DEATHDATE", "deathdate_identity"),
    )
    result = phase3_c1_eligibility(config, table="patients")
    assert result.admitted is True
    assert result.reasons == ()


def test_frozen_c1_observations_table_admits() -> None:
    config = _config(
        "observations",
        _hash_col("DATE", "observation_date_identity"),
        _hash_col("VALUE", "observation_value_identity"),
    )
    result = phase3_c1_eligibility(config, table="observations")
    assert result.admitted is True
    assert result.reasons == ()


# ---------------------------------------------------------------------------
# One test per coded rejection (Task 3.3 Step 1), each violating exactly one
# axis of an otherwise C1-admissible faker column.
# ---------------------------------------------------------------------------


def test_non_deterministic_faker_rejects_faker_not_deterministic() -> None:
    config = _config("t", _faker_col(deterministic=False))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("faker_not_deterministic:FIRST",)


def test_deterministic_omitted_rejects_faker_not_deterministic() -> None:
    config = _config("t", _faker_col(deterministic=None))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("faker_not_deterministic:FIRST",)


@pytest.mark.parametrize(
    "cardinality_mode",
    ["unique", "match_source_cardinality", "scale_source_cardinality"],
)
def test_non_partition_independent_cardinality_rejects_coded(cardinality_mode: str) -> None:
    config = _config("t", _faker_col(cardinality_mode=cardinality_mode))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == (
        f"faker_cardinality_not_partition_independent:FIRST:{cardinality_mode}",
    )


def test_missing_namespace_rejects_faker_namespace_required() -> None:
    config = _config("t", _faker_col(namespace=None))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("faker_namespace_required:FIRST",)


def test_missing_pool_size_rejects_faker_pool_size_required() -> None:
    config = _config("t", _faker_col(pool_size=None))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("faker_pool_size_required:FIRST",)


def test_nonpoolable_faker_rejects_provider_not_pool_native() -> None:
    # address_full is faker-backed, poolable=False (_real_registry.py).
    config = _config("t", _faker_col(provider="address_full"))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("provider_not_pool_native:FIRST:address_full",)


def test_poolable_non_c1_provider_rejects_provider_not_in_c1_allowlist() -> None:
    # person_name is faker-backed, poolable=True, but not one of the two C1
    # providers.
    assert "person_name" not in C1_PROVIDER_ALLOWLIST
    config = _config("t", _faker_col(provider="person_name"))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("provider_not_in_c1_allowlist:FIRST:person_name",)


def test_decoy_native_pool_native_provider_also_rejects_allowlist() -> None:
    # A decoy_native identifier is always pool_native (classify_provider),
    # but it is a different pool_native family than C1's faker providers.
    config = _config("t", _faker_col(provider="synthetic_ssn"))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("provider_not_in_c1_allowlist:FIRST:synthetic_ssn",)


def test_unregistered_provider_rejects_provider_reject_large() -> None:
    config = _config("t", _faker_col(provider="not_a_real_provider"))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("provider_reject_large:FIRST:not_a_real_provider",)


def test_vaulted_faker_column_rejects_faker_config_shape_unsupported() -> None:
    # Every other axis is C1-valid; only `vault: true` is added. The native
    # route never wires vault persistence (see the module docstring), so an
    # otherwise-admissible column must still reject.
    config = _config("t", _faker_col(vault=True))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("faker_config_shape_unsupported:FIRST:vault",)


def test_when_gated_faker_column_rejects_faker_config_shape_unsupported() -> None:
    # `when:` row gating has no wiring on the native pool route, so a
    # `when`-gated column would mask every row instead of the gate's matches.
    # Rejected for symmetry with vault (a raw dict can carry `when` even though
    # a PipelineConfig-validated mask column cannot).
    config = _config("t", _faker_col(when="AGE > 18"))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("faker_config_shape_unsupported:FIRST:when",)


def test_allow_collisions_with_deterministic_omitted_admits() -> None:
    # `allow_collisions: true` compiles to deterministic + reuse, so a column
    # the oracle resolves as deterministic-reuse must be ADMITTED here, not
    # mis-rejected as faker_not_deterministic (the reason string the raw
    # `deterministic` read alone would have produced).
    config = _config("t", _faker_col(deterministic=None, allow_collisions=True))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is True
    assert result.reasons == ()


def test_allow_collisions_with_conflicting_cardinality_mode_rejects() -> None:
    # allow_collisions forces reuse, but compile_plan REJECTS an explicit
    # non-reuse mode declared alongside it (allow_collisions_mode_conflict). The
    # predicate must mirror that, not silently coerce to reuse and admit.
    config = _config(
        "t", _faker_col(deterministic=None, allow_collisions=True, cardinality_mode="unique")
    )
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("allow_collisions_mode_conflict:FIRST:unique",)


def test_duplicate_faker_declarations_do_not_hide_an_unsafe_column() -> None:
    # A table can carry two declarations for one column name; a name-keyed dict
    # would let the valid second FIRST overwrite the unsafe first and admit.
    # Every declaration must be evaluated, so the non-deterministic one rejects.
    config = _config(
        "t",
        _faker_col("FIRST", deterministic=False),
        _faker_col("FIRST", provider="person_first_name", namespace="ns_first"),
    )
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert "faker_not_deterministic:FIRST" in result.reasons


def test_identical_unsafe_duplicate_declarations_report_the_reason_once() -> None:
    # Two identical non-deterministic FIRST declarations produce the same reason
    # string; it is reported ONCE (stable-deduped), not repeated. Admission is
    # still fail-closed either way.
    config = _config(
        "t",
        _faker_col("FIRST", deterministic=False),
        _faker_col("FIRST", deterministic=False),
    )
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("faker_not_deterministic:FIRST",)


# ---------------------------------------------------------------------------
# Totality: every live registry provider gets a defined, non-raising verdict.
# ---------------------------------------------------------------------------


def test_totality_over_live_provider_registry() -> None:
    registry = get_default_registry()
    providers = registry.known_providers()
    assert len(providers) > 0  # sanity: the registry actually enumerates something

    recognized_prefixes = (
        "provider_not_pool_native",
        "provider_not_in_c1_allowlist",
        "provider_reject_large",
        "composite_provider_multi_column",
    )
    for provider in sorted(providers):
        config = _config(
            "t",
            _faker_col("c", provider=provider, deterministic=True, namespace="ns", pool_size=100),
        )
        result = phase3_c1_eligibility(config, table="t")  # must never raise
        if provider in C1_PROVIDER_ALLOWLIST:
            assert result.admitted is True, provider
            assert result.reasons == (), provider
        else:
            assert result.admitted is False, provider
            assert result.reasons, provider
            for reason in result.reasons:
                assert reason.startswith(recognized_prefixes), (provider, reason)


# ---------------------------------------------------------------------------
# Cross-check: Phase 1's base predicate never admits faker; Phase 3 does.
# ---------------------------------------------------------------------------


def test_table_not_found_in_config_is_vacuously_admitted() -> None:
    # Mirrors native_route_eligibility's own convention for an absent table:
    # nothing to classify, so admit rather than guess. Also the only path
    # that exercises `_find_table`'s no-match loop iteration and its final
    # `return None`.
    config = {"tables": [{"name": "other", "columns": []}]}
    result = phase3_c1_eligibility(config, table="missing")
    assert result.admitted is True
    assert result.reasons == ()


def test_conflicting_pool_size_locations_surface_the_compiler_own_code() -> None:
    # top-level pool_size and provider_config.pool_size disagree:
    # resolve_pool_size raises pool_size_location_conflict, and
    # _faker_column_rejection surfaces the compiler's own code rather than
    # mis-attributing it to a JC-5 reason.
    config = _config(
        "t",
        {
            "name": "FIRST",
            "strategy": "faker",
            "provider": "person_first_name",
            "deterministic": True,
            "namespace": "ns",
            "pool_size": 100,
            "provider_config": {"pool_size": 200},
        },
    )
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("pool_size_location_conflict:FIRST",)


def test_phase3_admits_what_phase1_alone_rejects() -> None:
    config = _config("t", _faker_col())

    phase1 = native_route_eligibility(config, table="t")
    assert phase1.accepted is False
    assert any(r.startswith("no_native_kernel:FIRST:faker") for r in phase1.rejections)

    phase3 = phase3_c1_eligibility(config, table="t")
    assert phase3.admitted is True
    assert phase3.reasons == ()


# ---------------------------------------------------------------------------
# P3-T0 carried-forward survivor: dropping the caller's `profile` argument to
# `native_route_eligibility` (x_phase3_c1_eligibility__mutmut_9/_12) must
# change the admitted verdict. `native_route_eligibility` defers a hash
# column's input-type gate when `profile is None` (`hash_config_rejection`
# returns None, undecided rather than admitted-by-default); passing a real
# profile with an unresolvable dtype turns that deferral into a real
# rejection. A table with both an admissible faker column and one such hash
# column is admitted without a profile and rejected with one -- the one
# scenario the P3-T0 record found untested.
# ---------------------------------------------------------------------------


def _hash_profile(table: str, column: str, dtype: str) -> Profile:
    return Profile(
        schema_version=1,
        tables=(
            TableProfile(
                name=table,
                row_count=3,
                columns=(
                    ColumnProfile(
                        name=column,
                        dtype=dtype,
                        row_count=3,
                        null_count=0,
                        distinct_count=3,
                        sampled=False,
                        is_candidate_key_sampled=False,
                        declared_pk=False,
                        is_fk=False,
                        fk_target=None,
                        pii_class=None,
                    ),
                ),
            ),
        ),
        relationships=(),
        profiled_at=datetime(2026, 8, 30, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def test_dropping_the_profile_argument_silently_admits_an_unresolvable_hash_column() -> None:
    config = _config("t", _faker_col(), _hash_col("BAD", "bad_identity"))
    profile = _hash_profile("t", "BAD", "mixed")  # a dtype label the resolver never recognizes

    without_profile = phase3_c1_eligibility(config, table="t", profile=None)
    with_profile = phase3_c1_eligibility(config, table="t", profile=profile)

    assert without_profile.admitted is True
    assert without_profile.reasons == ()
    assert with_profile.admitted is False
    assert with_profile.reasons == ("mixed_object_not_native:BAD",)


# ---------------------------------------------------------------------------
# A non-faker column entry must not short-circuit the scan of the REST of
# the table's columns (a `continue`->`break` mutant would silently drop
# every later column's rejection).
#
# The sibling `not isinstance(col, dict)` guard a few lines above this one
# has NO reachable test through `phase3_c1_eligibility`: `base =
# native_route_eligibility(...)` runs first, over the SAME `table_cfg`
# columns list, and its own column loop has no such guard -- any non-dict
# entry crashes there (`col.get(...)` on a `str`) before this function's own
# loop is ever reached. A `continue`->`break` mutant on that dead guard is
# adjudicated unreachable-by-contract, not tested.
# ---------------------------------------------------------------------------


def test_a_non_faker_column_does_not_hide_a_later_unsafe_faker_column() -> None:
    config = _config(
        "t",
        _hash_col("H", "h_identity"),
        _faker_col("FIRST", deterministic=False),
    )
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert "faker_not_deterministic:FIRST" in result.reasons


# ---------------------------------------------------------------------------
# A composite-provider faker column must be excluded from Phase 3's own
# per-column check (native_route_eligibility already rejects it via
# `composite_provider_multi_column`) -- not merely admitted, but reported
# with EXACTLY that one reason, never a spurious second one from Phase 3
# re-processing it as if it were scalar.
# ---------------------------------------------------------------------------


def test_composite_provider_faker_column_reports_only_the_base_composite_reason() -> None:
    registry = get_default_registry()
    composite_providers = [
        p
        for p in registry.known_providers()
        if registry.get_capabilities(p).backend_type == "composite"
    ]
    assert composite_providers  # sanity: the registry actually has one
    provider = composite_providers[0]

    config = _config("t", _faker_col("C", provider=provider))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == (f"composite_provider_multi_column:C:{provider}",)


def test_composite_provider_faker_column_does_not_hide_a_later_unsafe_faker_column() -> None:
    registry = get_default_registry()
    composite_providers = [
        p
        for p in registry.known_providers()
        if registry.get_capabilities(p).backend_type == "composite"
    ]
    provider = composite_providers[0]

    config = _config(
        "t",
        _faker_col("C", provider=provider),
        _faker_col("FIRST", deterministic=False),
    )
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert f"composite_provider_multi_column:C:{provider}" in result.reasons
    assert "faker_not_deterministic:FIRST" in result.reasons


# ---------------------------------------------------------------------------
# A duplicate column NAME across two DIFFERENT strategies (not two faker
# declarations) must not cross-contaminate: stripping the base predicate's
# `no_native_kernel:<name>:faker` entry for a name in `faker_names` must not
# also strip an unrelated `no_native_kernel:<name>:<other-strategy>` entry
# for the SAME name (an `and`->`or` mutant in the dedup filter would).
# ---------------------------------------------------------------------------


def test_duplicate_name_across_faker_and_another_no_kernel_strategy_keeps_both_verdicts() -> None:
    config = _config(
        "t",
        _faker_col("X"),  # otherwise perfectly C1-admissible
        {"name": "X", "strategy": "date_shift", "namespace": "ns2"},
    )
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("no_native_kernel:X:date_shift",)


# ---------------------------------------------------------------------------
# A column name containing a colon is legal (`ColumnConfig.name: str` has no
# charset restriction); the coded-reason dedup must still correctly parse the
# base predicate's `no_native_kernel:<name>:faker` entry and strip it for an
# otherwise-admissible column, not mis-split the name as part of the
# strategy token.
# ---------------------------------------------------------------------------


def test_colon_bearing_column_name_still_admits_cleanly() -> None:
    config = _config("t", _faker_col("FIRST:ALT"))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is True
    assert result.reasons == ()


# ---------------------------------------------------------------------------
# `allow_collisions: true` alongside an EXPLICIT `cardinality_mode: reuse`
# is compatible (not a conflict) and must admit: the mode-conflict check's
# `!= "reuse"` string comparison must compare against the real literal, not
# a corrupted one.
# ---------------------------------------------------------------------------


def test_allow_collisions_with_explicit_reuse_mode_admits_no_conflict() -> None:
    config = _config(
        "t", _faker_col(deterministic=None, allow_collisions=True, cardinality_mode="reuse")
    )
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is True
    assert result.reasons == ()


# ---------------------------------------------------------------------------
# An empty-string provider is a distinct fail-closed path from a missing/
# non-string provider (`not isinstance(provider, str) or not provider`, not
# `and`): both must reject, but the coded reason's exact text differs
# (`{provider!r}` at this early guard vs. plain `{provider}` from
# `classify_provider`'s own reject_large branch further down).
# ---------------------------------------------------------------------------


def test_empty_string_provider_rejects_with_repr_formatted_reason() -> None:
    config = _config("t", _faker_col(provider=""))
    result = phase3_c1_eligibility(config, table="t")
    assert result.admitted is False
    assert result.reasons == ("provider_reject_large:FIRST:''",)


# ---------------------------------------------------------------------------
# Property sweep over the JC-5 config shape (Task plan candidate gap 1): the
# admitted set is EXACTLY the deterministic, source-keyed,
# partition-independent configs -- reconstructed independently from the
# frozen contract (`_faker_column_rejection`'s own check order: the
# allow_collisions/mode conflict, then deterministic, then cardinality mode,
# then namespace, then pool_size), over every combination of the five axes,
# for a fixed C1-allowlisted provider so only the JC-5 axes vary.
# ---------------------------------------------------------------------------

_CARDINALITY_MODES = [
    None,
    "reuse",
    "unique",
    "match_source_cardinality",
    "scale_source_cardinality",
]


def _expected_jc5_admitted(
    *,
    deterministic: bool | None,
    cardinality_mode: str | None,
    has_namespace: bool,
    has_pool_size: bool,
    allow_collisions: bool,
) -> bool:
    if allow_collisions and cardinality_mode is not None and cardinality_mode != "reuse":
        return False  # allow_collisions_mode_conflict
    effective_deterministic = allow_collisions or bool(deterministic)
    if not effective_deterministic:
        return False  # faker_not_deterministic
    effective_mode = "reuse" if allow_collisions else (cardinality_mode or "reuse")
    if effective_mode != "reuse":
        return False  # faker_cardinality_not_partition_independent
    if not has_namespace:
        return False  # faker_namespace_required
    return has_pool_size  # else faker_pool_size_required


@given(
    deterministic=st.sampled_from([True, False, None]),
    cardinality_mode=st.sampled_from(_CARDINALITY_MODES),
    has_namespace=st.booleans(),
    has_pool_size=st.booleans(),
    allow_collisions=st.booleans(),
)
@settings(max_examples=300, deadline=None)
def test_jc5_admitted_set_is_exactly_deterministic_source_keyed_partition_independent(
    deterministic: bool | None,
    cardinality_mode: str | None,
    has_namespace: bool,
    has_pool_size: bool,
    allow_collisions: bool,
) -> None:
    col = _faker_col(
        provider="person_first_name",
        deterministic=deterministic,
        namespace="ns_first" if has_namespace else None,
        pool_size=10_000 if has_pool_size else None,
        cardinality_mode=cardinality_mode,
        allow_collisions=allow_collisions,
    )
    config = _config("t", col)
    result = phase3_c1_eligibility(config, table="t")

    expected = _expected_jc5_admitted(
        deterministic=deterministic,
        cardinality_mode=cardinality_mode,
        has_namespace=has_namespace,
        has_pool_size=has_pool_size,
        allow_collisions=allow_collisions,
    )
    assert result.admitted is expected, (
        deterministic,
        cardinality_mode,
        has_namespace,
        has_pool_size,
        allow_collisions,
        result.reasons,
    )
    assert result.admitted == (result.reasons == ())
