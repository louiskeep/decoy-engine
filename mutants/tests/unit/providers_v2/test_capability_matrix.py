"""CapabilityMatrix registry-load validation tests (S4 spec §4 + cold-read M3 + M5)."""

from __future__ import annotations

import faker as faker_module
import pytest
from pydantic import ValidationError

from decoy_engine.providers_v2 import CapabilityMatrix, get_default_registry
from decoy_engine.providers_v2._real_registry import get_default_catalog


class TestPerCatalogEntry:
    def test_catalog_has_24_entries(self) -> None:
        """Post-S6 swap (per S6 spec §7): the 5 synthetic identifiers
        (ssn, ein, npi, ndc, mrn) moved out of the Faker catalog and into
        DecoyNativeAdapter bindings in get_default_registry(). Faker-bound
        catalog is now 19; full registry is 24."""
        assert len(get_default_catalog()) == 19

    def test_full_registry_has_34_entries(self) -> None:
        """Full registry: 19 Faker + 5 DecoyNative (S6) + 2 composite
        (S8) + 4 MG-1 S4 domain providers + 4 MG-4 composites = 34.

        Drift guard: this count cell mirrors the canary cell at
        `test_documented_allowlist_matches_registry` (MG-8 Step 5).
        A new provider registration updates both in lockstep."""
        registry = get_default_registry()
        assert len(registry.known_providers()) == 34

    def test_all_entries_validate_at_import(self) -> None:
        """Pydantic raises at construction if any field is missing/mistyped.
        Importing _real_registry constructs the catalog; if it imported
        without error, every entry is valid."""
        catalog = get_default_catalog()
        for cap in catalog:
            assert isinstance(cap, CapabilityMatrix)

    def test_every_entry_has_all_14_fields(self) -> None:
        """Per S4 spec §4 M5: 14 fields including backend_version."""
        catalog = get_default_catalog()
        for cap in catalog:
            data = cap.model_dump()
            assert set(data.keys()) == {
                "provider",
                "backend_type",
                "backend_version",
                "supports_deterministic",
                "supports_uniqueness",
                "supports_value_reuse",
                "preserves_source_cardinality",
                "participates_in_fk_pk",
                "poolable",
                "supported_locales",
                "supports_coherent_link",
                "format_regex",
                "blocklist_validators",
                "fallback_behavior",
            }


class TestS4ClosePerFieldDefaults:
    """Per S4 spec §4 M3 table: S4 declares some fields permanently True/False
    at S4 close; later sprints fill the True occurrences for their own surfaces."""

    def test_every_entry_supports_deterministic_false(self) -> None:
        """Per H2 PO call: S4 owns zero supports_deterministic=True
        occurrences; S5/S6/S7 light up True for their own additions."""
        for cap in get_default_catalog():
            assert cap.supports_deterministic is False

    def test_every_entry_supports_coherent_link_false(self) -> None:
        """S4 declares False; S8 lights up True for composite_name_email etc."""
        for cap in get_default_catalog():
            assert cap.supports_coherent_link is False

    def test_every_entry_format_regex_is_none(self) -> None:
        """S4 declares None; S6 sets per-identifier regex for the 4 synthetic
        identifier swaps."""
        for cap in get_default_catalog():
            assert cap.format_regex is None

    def test_every_entry_has_empty_blocklist_validators(self) -> None:
        """S4 declares (); S6 registers per-identifier blocklist validators."""
        for cap in get_default_catalog():
            assert cap.blocklist_validators == ()

    def test_every_entry_fallback_is_fail_plan_compile(self) -> None:
        """Per non-negotiable on no-silent-downgrade: every provider defaults
        to fail_plan_compile."""
        for cap in get_default_catalog():
            assert cap.fallback_behavior == "fail_plan_compile"

    def test_every_entry_backend_version_equals_installed_faker_version(self) -> None:
        """Per M5: backend_version stamp = faker.__version__ at S4 close."""
        expected = faker_module.VERSION
        for cap in get_default_catalog():
            assert cap.backend_version == expected, (
                f"provider {cap.provider!r} declares "
                f"backend_version={cap.backend_version!r}; "
                f"expected {expected!r}"
            )

    def test_every_entry_backend_type_is_faker(self) -> None:
        """At S4 close every entry is bound to FakerAdapter; S6 swaps 4 to
        decoy_native; S7 may swap some to mimesis."""
        for cap in get_default_catalog():
            assert cap.backend_type == "faker"


class TestPoolableSubset:
    """Per S4 spec §4 M3: 12+ PII-shaped entries declare poolable=True."""

    def test_pii_providers_poolable(self) -> None:
        poolable_subset = {cap.provider for cap in get_default_catalog() if cap.poolable}
        # Post-S6 Faker catalog: 19 entries; poolable subset shrinks because the
        # 5 synthetic identifiers (ssn/ein/npi/ndc/mrn) moved to DecoyNative
        # with poolable=False per S6 §3.1.
        assert "person_email" in poolable_subset
        assert "address_zip" in poolable_subset
        assert "synthetic_member_id" in poolable_subset  # still Faker-bound
        assert "lorem_text" not in poolable_subset
        assert "uuid" not in poolable_subset
        assert "random_int_range" not in poolable_subset
        assert "address_full" not in poolable_subset
        # Post-S6: the 5 swapped identifiers are no longer in the Faker
        # catalog (they're DecoyNative-bound in get_default_registry()).
        assert "synthetic_ssn" not in poolable_subset
        assert "synthetic_npi" not in poolable_subset


class TestExtraFieldsForbidden:
    """Per S4 spec §4: extra='forbid' catches typos at construction time."""

    def test_construction_with_unknown_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            CapabilityMatrix(
                provider="x",
                backend_type="faker",
                backend_version="0.0.0",
                supports_deterministic=False,
                supports_uniqueness=True,
                supports_value_reuse=True,
                preserves_source_cardinality=False,
                participates_in_fk_pk=False,
                poolable=False,
                supported_locales=("en_US",),
                supports_coherent_link=False,
                format_regex=None,
                blocklist_validators=(),
                fallback_behavior="fail_plan_compile",
                unknown_field="should fail",  # type: ignore[call-arg]
            )


class TestRegistryWiring:
    def test_registry_get_capabilities_returns_catalog_entry(self) -> None:
        # synthetic_member_id stays Faker-bound regardless of mimesis
        # adoption state; person_* providers rebind when the extra is
        # installed.
        registry = get_default_registry()
        cap = registry.get_capabilities("synthetic_member_id")
        assert cap.provider == "synthetic_member_id"
        assert cap.backend_type == "faker"

    def test_registry_get_capabilities_unknown_raises(self) -> None:
        from decoy_engine.providers_v2 import ProviderError

        registry = get_default_registry()
        with pytest.raises(ProviderError) as excinfo:
            registry.get_capabilities("not_a_provider")
        assert excinfo.value.code == "unknown_provider"
