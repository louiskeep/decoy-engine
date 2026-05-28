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
    return get_default_registry().get_adapter("person_email")


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


class TestSeedStability:
    """S4 spec §10 adapter conformance: seed-stability for Faker means same
    Faker.seed -> same Faker outputs. This is Faker's own contract; the
    deterministic-mode-via-source-value contract is S5's PoolAdapter."""

    def test_faker_seed_produces_stable_output(self) -> None:
        """Faker(seed) -> seeded RNG -> same first output across two instances."""
        import faker as faker_module

        f1 = faker_module.Faker()
        f1.seed_instance(42)
        out1 = f1.email()

        f2 = faker_module.Faker()
        f2.seed_instance(42)
        out2 = f2.email()

        assert out1 == out2
