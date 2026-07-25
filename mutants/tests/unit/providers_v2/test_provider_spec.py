"""ProviderSpec validation tests (S4 spec §3 + cold-read M2 + L3)."""

from __future__ import annotations

import pytest

from decoy_engine.providers_v2 import ProviderError, ProviderSpec


class TestDeterministicInvariants:
    """deterministic=True requires (namespace, seed) both non-None."""

    def test_deterministic_without_namespace_raises(self) -> None:
        with pytest.raises(ProviderError) as excinfo:
            ProviderSpec(
                locale="en_US",
                deterministic=True,
                namespace=None,
                seed=b"\x00" * 8,
            )
        assert excinfo.value.code == "deterministic_requires_namespace_and_seed"

    def test_deterministic_without_seed_raises(self) -> None:
        with pytest.raises(ProviderError) as excinfo:
            ProviderSpec(
                locale="en_US",
                deterministic=True,
                namespace="ns",
                seed=None,
            )
        assert excinfo.value.code == "deterministic_requires_namespace_and_seed"

    def test_deterministic_with_empty_namespace_raises(self) -> None:
        with pytest.raises(ProviderError) as excinfo:
            ProviderSpec(
                locale="en_US",
                deterministic=True,
                namespace="",
                seed=b"\x00" * 8,
            )
        assert excinfo.value.code == "namespace_empty"

    def test_deterministic_with_wrong_seed_length_raises(self) -> None:
        with pytest.raises(ProviderError) as excinfo:
            ProviderSpec(
                locale="en_US",
                deterministic=True,
                namespace="ns",
                seed=b"abc",
            )
        assert excinfo.value.code == "seed_wrong_length"

    def test_deterministic_valid_passes(self) -> None:
        spec = ProviderSpec(
            locale="en_US",
            deterministic=True,
            namespace="ns",
            seed=b"\x00" * 8,
        )
        assert spec.deterministic is True


class TestNonDeterministicSpec:
    def test_non_deterministic_with_no_namespace_no_seed(self) -> None:
        spec = ProviderSpec(
            locale="en_US",
            deterministic=False,
            namespace=None,
            seed=None,
        )
        assert spec.deterministic is False

    def test_seed_wrong_length_raises_even_when_non_deterministic(self) -> None:
        """Per S4 spec §3 M2: seed length check applies whenever seed is
        set, regardless of deterministic flag (catches misuse early)."""
        with pytest.raises(ProviderError) as excinfo:
            ProviderSpec(
                locale="en_US",
                deterministic=False,
                namespace=None,
                seed=b"abc",
            )
        assert excinfo.value.code == "seed_wrong_length"


class TestExtraField:
    def test_extra_defaults_to_empty_dict(self) -> None:
        spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None)
        assert spec.extra == {}

    def test_extra_carries_provider_kwargs(self) -> None:
        spec = ProviderSpec(
            locale="en_US",
            deterministic=False,
            namespace=None,
            seed=None,
            extra={"domain": "example.com"},
        )
        assert spec.extra["domain"] == "example.com"


class TestFrozenDataclass:
    """ProviderSpec is a frozen dataclass: attribute reassignment fails."""

    def test_field_reassignment_raises(self) -> None:
        from dataclasses import FrozenInstanceError

        spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None)
        with pytest.raises(FrozenInstanceError):
            spec.deterministic = True  # type: ignore[misc]

    def test_unhashable_because_extra_is_dict(self) -> None:
        """Per S4 spec §3 L2: ProviderSpec is unhashable (dict field).
        Callers that need a cache key derive one via canonical JSON."""
        spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None)
        with pytest.raises(TypeError):
            hash(spec)
