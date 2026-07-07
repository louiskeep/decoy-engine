"""S7 (Sprint 2 honesty pack): public faker-provider accessor (follow-up #11).

TDD: written before the implementation. `list_generate_faker_providers`
returns the sorted union of the reflected public provider names (filtered
through the EXISTING reflection denylist, `internal/faker_setup.py`) and
currently registered custom providers, reusing `get_faker_providers` (no
second denylist).
"""

from __future__ import annotations

from decoy_engine.providers import (
    list_generate_faker_providers,
    register_faker_provider,
    unregister_faker_provider,
)


class TestListGenerateFakerProviders:
    def test_returns_sorted_tuple(self) -> None:
        result = list_generate_faker_providers()
        assert isinstance(result, tuple)
        assert result == tuple(sorted(result))

    def test_contains_representative_providers(self) -> None:
        result = list_generate_faker_providers()
        assert "name" in result
        assert "email" in result

    def test_excludes_plumbing_via_denylist(self) -> None:
        result = list_generate_faker_providers()
        assert "seed_instance" not in result
        assert "seed" not in result
        assert "add_provider" not in result

    def test_excludes_private_names(self) -> None:
        result = list_generate_faker_providers()
        assert not any(name.startswith("_") for name in result)

    def test_custom_provider_appears_after_registration(self) -> None:
        register_faker_provider("sp2_test_custom_provider", lambda fake: "x")
        try:
            result = list_generate_faker_providers()
            assert "sp2_test_custom_provider" in result
        finally:
            unregister_faker_provider("sp2_test_custom_provider")

    def test_custom_provider_removed_after_unregistration(self) -> None:
        register_faker_provider("sp2_test_custom_provider2", lambda fake: "x")
        unregister_faker_provider("sp2_test_custom_provider2")
        result = list_generate_faker_providers()
        assert "sp2_test_custom_provider2" not in result

    def test_locale_argument_honored(self) -> None:
        # en_GB and en_US both expose "name"; locale must not raise and must
        # still surface standard providers.
        result_us = list_generate_faker_providers("en_US")
        result_gb = list_generate_faker_providers("en_GB")
        assert "name" in result_us
        assert "name" in result_gb

    def test_top_level_export(self) -> None:
        import decoy_engine

        assert "list_generate_faker_providers" in decoy_engine.__all__
        assert decoy_engine.list_generate_faker_providers is list_generate_faker_providers
