"""FakerAdapter behavior tests (S4 spec §7 + §10 adapter conformance gate).

At S4 close FakerAdapter is non-deterministic for every catalog entry
per the H2 PO call. Deterministic-mode requests raise capability_violation
directing callers to S5's PoolAdapter or S6's DecoyNativeAdapter.
"""

from __future__ import annotations

import re

import pytest

from decoy_engine.providers_v2 import (
    ProviderError,
    ProviderSpec,
    get_default_registry,
)


def _spec(**overrides) -> ProviderSpec:
    defaults = dict(locale="en_US", deterministic=False, namespace=None, seed=None)
    defaults.update(overrides)
    return ProviderSpec(**defaults)


def _adapter():
    # This module tests FakerAdapter specifically. Fetch it via a provider
    # that stays Faker-bound regardless of mimesis adoption state
    # (person_* providers rebind to MimesisAdapter when the extra is
    # installed; synthetic_member_id is not an adoption candidate).
    return get_default_registry().get_adapter("synthetic_member_id")


class TestNonDeterministicGenerate:
    def test_generate_person_email_returns_email_shaped_string(self) -> None:
        out = _adapter().generate("person_email", spec=_spec())
        assert isinstance(out, str)
        assert re.match(r"[^@]+@[^@]+\.[^@]+", out)

    def test_generate_person_name_returns_string(self) -> None:
        out = _adapter().generate("person_name", spec=_spec())
        assert isinstance(out, str) and len(out) > 0

    def test_generate_address_zip_returns_string(self) -> None:
        out = _adapter().generate("address_zip", spec=_spec())
        assert isinstance(out, str) and len(out) > 0

    def test_generate_synthetic_member_id_returns_string(self) -> None:
        """Post-S6: synthetic_ssn is now DecoyNative-bound; test a still-Faker-bound
        synthetic identifier (synthetic_member_id) instead."""
        out = _adapter().generate("synthetic_member_id", spec=_spec())
        assert isinstance(out, str) and len(out) > 0

    def test_generate_unknown_provider_raises_adapter_error(self) -> None:
        from decoy_engine.providers_v2 import AdapterError

        with pytest.raises(AdapterError) as excinfo:
            _adapter().generate("not_a_real_provider", spec=_spec())
        assert excinfo.value.code == "unknown_provider"


class TestGenerateBatch:
    def test_generate_batch_returns_count_values(self) -> None:
        out = _adapter().generate_batch("person_email", spec=_spec(), count=10)
        assert len(out) == 10

    def test_generate_batch_returns_mostly_distinct_emails(self) -> None:
        """Faker's email pool is wider than 10; expect mostly distinct."""
        out = _adapter().generate_batch("person_email", spec=_spec(), count=10)
        # Allow some duplicates from Faker's pool but expect >50% unique.
        assert len(set(out)) >= 6

    def test_generate_batch_deterministic_raises(self) -> None:
        with pytest.raises(ProviderError) as excinfo:
            _adapter().generate_batch(
                "person_email",
                spec=_spec(deterministic=True, namespace="ns", seed=b"\x00" * 8),
                count=10,
            )
        assert excinfo.value.code == "capability_violation"


class TestDeterministicRejection:
    """H2: every catalog entry declares supports_deterministic=False at S4
    close. FakerAdapter rejects deterministic requests with a clear error."""

    def test_deterministic_generate_raises_capability_violation(self) -> None:
        with pytest.raises(ProviderError) as excinfo:
            _adapter().generate(
                "person_email",
                spec=_spec(deterministic=True, namespace="ns", seed=b"\x00" * 8),
            )
        assert excinfo.value.code == "capability_violation"
        # Error message points the caller at S5 PoolAdapter or S6 DecoyNativeAdapter
        msg = excinfo.value.message
        assert "PoolAdapter" in msg or "S5" in msg

    @pytest.mark.parametrize(
        "provider",
        [
            "person_name",
            "synthetic_ssn",
            "synthetic_npi",
            "address_zip",
            "uuid",
        ],
    )
    def test_every_catalog_provider_rejects_deterministic_at_s4(self, provider: str) -> None:
        with pytest.raises(ProviderError) as excinfo:
            _adapter().generate(
                provider,
                spec=_spec(deterministic=True, namespace="ns", seed=b"\x00" * 8),
            )
        assert excinfo.value.code == "capability_violation"


class TestCapabilityMatrixAccess:
    def test_adapter_capability_matrix_returns_registry_entry(self) -> None:
        cap = _adapter().capability_matrix("person_email")
        assert cap.provider == "person_email"
        assert cap.supports_deterministic is False

    def test_adapter_capability_matrix_unknown_raises(self) -> None:
        with pytest.raises(ProviderError) as excinfo:
            _adapter().capability_matrix("not_a_real_provider")
        assert excinfo.value.code == "unknown_provider"


# S4 spec §10 adapter conformance (seed-stability) for FakerAdapter is now
# exercised through the shared harness in test_adapter_conformance.py
# (engine-v2 S7; closes MEDIUM-ADAPTER-CONFORMANCE-1). The bespoke
# TestSeedStability class was re-pointed there so every adapter family runs
# through one gate rather than per-adapter copies.


def test_stateful_faker_names_stay_denylisted_for_determinism() -> None:
    # P5 invariant: the reflection surface must never expose Faker names whose
    # output depends on process/instance state (`unique`, `random`, `seed`).
    # Poolable byte-identity across concurrent/rebuilt pools relies on this;
    # if one leaked in, fresh-per-build isolation would change its sequence.
    from decoy_engine.internal.faker_setup import _FAKER_DENYLIST

    for name in ("unique", "random", "seed", "seed_instance"):
        assert name in _FAKER_DENYLIST


class TestPersonProvidersSurviveFakerVersion:
    """Faker 40.31.0 migration regression guard (2026-09-25).

    The pin was held at 40.23.0 since 2026-07-23 on the belief that 40.31.0
    silently drops person_name/first/last/full/email from the registry
    (commit ad18dbaf). Investigation found that claim does not reproduce:
    `dir(Faker())`'s reflected attribute set is byte-identical across the
    two versions, and this file's own `test_generate_person_*` tests already
    exercise the V2 catalog path. This class covers the two things those
    didn't: the raw Faker methods the V1 + V2 layers both bind to
    (`person_name`/`first`/`last`/`full`/`email`'s underlying
    `name`/`first_name`/`last_name`/`email`), and the V1 reflection path
    (`get_faker_providers`), so a real future provider-drop regression on
    either layer fails loudly instead of needing a manual lock-regen
    diagnosis again.
    """

    def test_underlying_faker_methods_present_on_instance(self) -> None:
        from faker import Faker

        fake = Faker()
        for method_name in ("name", "first_name", "last_name", "email"):
            assert hasattr(fake, method_name), (
                f"Faker instance lost {method_name!r}; check the installed "
                "faker version against the pyproject.toml pin"
            )
            assert callable(getattr(fake, method_name))

    def test_v1_reflection_exposes_the_five_person_providers(self) -> None:
        from decoy_engine.internal.faker_setup import get_faker_providers, make_faker

        fake = make_faker()
        providers = get_faker_providers(fake)
        for reflected_name in ("name", "first_name", "last_name", "email"):
            assert reflected_name in providers, (
                f"V1 reflection dropped {reflected_name!r} from get_faker_providers()"
            )

    def test_v2_registry_resolves_all_five_semantic_names(self) -> None:
        # person_full_name maps to the same underlying `name` method as
        # person_name (see _FAKER_METHOD_MAP); both are exercised here to
        # match the roadmap's five-name framing (person_name/first/last/
        # full/email) even though full and name share a backend call.
        for semantic_name in (
            "person_name",
            "person_first_name",
            "person_last_name",
            "person_full_name",
            "person_email",
        ):
            out = _adapter().generate(semantic_name, spec=_spec())
            assert isinstance(out, str) and out, (
                f"V2 registry failed to generate a value for {semantic_name!r}"
            )
