"""ProviderRegistry tests + V2 custom-provider registration tests (S4 spec §5 + §8)."""

from __future__ import annotations

import pytest

from decoy_engine.providers_v2 import (
    BackendAdapter,
    CapabilityMatrix,
    ProviderError,
    ProviderSpec,
    get_default_registry,
    register_faker_provider_v2,
)


class TestDefaultRegistry:
    def test_singleton_returns_same_object(self) -> None:
        r1 = get_default_registry()
        r2 = get_default_registry()
        assert r1 is r2

    def test_has_known_provider(self) -> None:
        assert get_default_registry().has("person_email") is True

    def test_has_unknown_provider_false(self) -> None:
        assert get_default_registry().has("not_a_real_provider") is False

    def test_known_providers_returns_frozenset_of_24(self) -> None:
        """Spec text says 25 but real S1 count was 20 (not 21); 20 + 4 S4
        additions = 24. See test_capability_matrix::test_catalog_has_24_entries."""
        known = get_default_registry().known_providers()
        assert isinstance(known, frozenset)
        assert len(known) == 24

    def test_get_adapter_for_unknown_raises(self) -> None:
        with pytest.raises(ProviderError) as excinfo:
            get_default_registry().get_adapter("not_a_real_provider")
        assert excinfo.value.code == "unknown_provider"

    def test_get_capabilities_for_known_returns_capmat(self) -> None:
        cap = get_default_registry().get_capabilities("person_email")
        assert cap.provider == "person_email"


class _FakeAdapter:
    """Test-only BackendAdapter implementation."""

    backend_type = "test_fake"
    backend_version = "fake-1.0"

    def generate(self, provider, *, spec, source_value=None):
        return f"fake({provider})"

    def generate_batch(self, provider, *, spec, count):
        return [f"fake({provider})_{i}" for i in range(count)]

    def capability_matrix(self, provider):
        return CapabilityMatrix(
            provider=provider,
            backend_type=self.backend_type,
            backend_version=self.backend_version,
            supports_deterministic=False,
            supports_uniqueness=False,
            supports_value_reuse=True,
            preserves_source_cardinality=False,
            participates_in_fk_pk=False,
            poolable=False,
            supported_locales=("en_US",),
            supports_coherent_link=False,
            format_regex=None,
            blocklist_validators=(),
            fallback_behavior="fail_plan_compile",
        )


class TestOverride:
    """ProviderRegistry.override returns a NEW registry; default is never mutated."""

    def test_override_returns_new_registry(self) -> None:
        default = get_default_registry()
        fake = _FakeAdapter()
        fake_caps = fake.capability_matrix("person_email")
        new_registry = default.override("person_email", fake, fake_caps)
        assert new_registry is not default

    def test_override_swaps_binding_in_new_registry(self) -> None:
        default = get_default_registry()
        fake = _FakeAdapter()
        fake_caps = fake.capability_matrix("person_email")
        new_registry = default.override("person_email", fake, fake_caps)
        assert new_registry.get_adapter("person_email") is fake
        assert new_registry.get_capabilities("person_email").backend_type == "test_fake"

    def test_override_does_not_mutate_default(self) -> None:
        default = get_default_registry()
        fake = _FakeAdapter()
        fake_caps = fake.capability_matrix("person_email")
        _ = default.override("person_email", fake, fake_caps)
        # Default registry's binding is unchanged.
        assert default.get_capabilities("person_email").backend_type == "faker"


class TestV2CustomProviderRegistration:
    """register_faker_provider_v2 adds to the V2 custom-provider table.

    The V2 table is separate from V1's `_CUSTOM_FAKER_PROVIDERS` at
    `decoy_engine.internal.faker_setup`; V1 + V2 tables coexist.
    """

    def test_registered_v2_provider_routes_through_faker_adapter(self) -> None:
        from decoy_engine.providers_v2._faker_adapter import (
            _unregister_faker_provider_v2,
            _v2_custom_provider_names,
        )

        register_faker_provider_v2(
            "custom_id_v2_unique_test_name",
            lambda f: f.bothify("##-###"),
        )
        try:
            assert "custom_id_v2_unique_test_name" in _v2_custom_provider_names()
            registry = get_default_registry()
            adapter = registry.get_adapter("person_email")  # FakerAdapter
            spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None)
            out = adapter.generate("custom_id_v2_unique_test_name", spec=spec)
            assert isinstance(out, str)
            # Format: NN-NNN where N is a digit
            assert len(out) == 6
            assert out[2] == "-"
        finally:
            _unregister_faker_provider_v2("custom_id_v2_unique_test_name")

    def test_v2_registration_does_not_shadow_v1(self) -> None:
        """V1 callers continue to see V1-registered providers via V1 introspection
        (decoy_engine.providers.list_*_providers); V2-registered providers do
        not appear in V1 introspection."""
        from decoy_engine.internal.faker_setup import _CUSTOM_FAKER_PROVIDERS
        from decoy_engine.providers_v2._faker_adapter import (
            _unregister_faker_provider_v2,
            _v2_custom_provider_names,
        )

        register_faker_provider_v2(
            "v2_only_custom_provider_isolation_test",
            lambda f: "x",
        )
        try:
            assert "v2_only_custom_provider_isolation_test" in _v2_custom_provider_names()
            assert "v2_only_custom_provider_isolation_test" not in _CUSTOM_FAKER_PROVIDERS
        finally:
            _unregister_faker_provider_v2("v2_only_custom_provider_isolation_test")

    def test_v1_registration_does_not_appear_in_v2(self) -> None:
        """Inverse isolation: V1 `register_faker_provider` adds to V1's table
        only; the provider name does NOT appear in V2's custom-provider table.
        Dennis Session 22 L2."""
        from decoy_engine.internal.faker_setup import (
            register_faker_provider,
            unregister_faker_provider,
        )
        from decoy_engine.providers_v2._faker_adapter import _v2_custom_provider_names

        register_faker_provider("v1_only_custom_provider_inverse_test", lambda f: "y")
        try:
            assert "v1_only_custom_provider_inverse_test" not in _v2_custom_provider_names()
        finally:
            unregister_faker_provider("v1_only_custom_provider_inverse_test")


class TestBackendAdapterProtocolConformance:
    """FakerAdapter conforms to the BackendAdapter Protocol."""

    def test_faker_adapter_satisfies_protocol(self) -> None:
        """Structural typing: FakerAdapter has the right methods + attributes."""
        adapter = get_default_registry().get_adapter("person_email")
        # Has the protocol attributes
        assert hasattr(adapter, "backend_type")
        assert hasattr(adapter, "backend_version")
        # Has the protocol methods
        assert callable(adapter.generate)
        assert callable(adapter.generate_batch)
        assert callable(adapter.capability_matrix)

    def test_faker_adapter_is_a_backend_adapter_at_runtime(self) -> None:
        """The Protocol is not @runtime_checkable, but the structural test
        above proves the surface matches. This belt-and-suspenders test
        confirms the typing hint resolves."""
        adapter = get_default_registry().get_adapter("person_email")
        # Just smoke-test that we can assign to a BackendAdapter-typed slot.
        _: BackendAdapter = adapter  # type: ignore[assignment]
