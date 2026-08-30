"""Task 3.3: `phase3_c1_eligibility`, the config-aware Phase 3 admission query.

Covers the JC-5 precondition split into distinct coded reasons (Task 3.1's
`faker_pool_precondition_met` collapses the same checks into one bool; this
predicate names each one), the C1 provider allowlist, the one config-shape
gap the native route cannot honor (`vault: true`), totality over the live
provider registry, and the layering cross-check against
`native_route_eligibility`.
"""

from __future__ import annotations

import pytest

from decoy_engine.execution.native._phase3_eligibility import (
    C1_PROVIDER_ALLOWLIST,
    phase3_c1_eligibility,
)
from decoy_engine.execution.native._plan import native_route_eligibility
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


def test_phase3_admits_what_phase1_alone_rejects() -> None:
    config = _config("t", _faker_col())

    phase1 = native_route_eligibility(config, table="t")
    assert phase1.accepted is False
    assert any(r.startswith("no_native_kernel:FIRST:faker") for r in phase1.rejections)

    phase3 = phase3_c1_eligibility(config, table="t")
    assert phase3.admitted is True
    assert phase3.reasons == ()
