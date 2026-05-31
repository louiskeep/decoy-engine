"""MG-1 integration smoke (Dennis H1 close, 2026-06-01).

Pins the registry-level integration that the unit tests (which
instantiate adapters directly) missed:

- The four new MG-1 S4 generators (PAN / ICD10 / IBAN / CUSIP) ARE
  registered in the default ProviderRegistry. Without this, a
  pipeline config using `provider: synthetic_pan` etc would 404 at
  plan-compile with `unknown_provider`.
- Each new provider's adapter returns a validator-passing value via
  the registry-mediated lookup path.

The narrow Dennis BLOCKER (B1) was that the four adapters existed
as classes but weren't bound in `get_default_registry`; this module
is the regression cell that catches a future repeat.
"""

from __future__ import annotations

import pytest

from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2.identifiers._cusip import _is_valid_cusip
from decoy_engine.providers_v2.identifiers._pan import _is_valid_pan
from decoy_engine.storm.detectors import _iban_valid, _icd10_valid


_NEW_PROVIDERS_MG1_S4 = (
    "synthetic_pan",
    "synthetic_icd10",
    "synthetic_iban",
    "synthetic_cusip",
)


_SEED = (0x0123456789).to_bytes(8, "big")


def _spec(*, deterministic: bool = False, namespace: str | None = None, seed: bytes | None = None) -> ProviderSpec:
    return ProviderSpec(
        locale="en_US",
        deterministic=deterministic,
        namespace=namespace,
        seed=seed,
    )


_VALIDATOR_BY_PROVIDER = {
    "synthetic_pan": _is_valid_pan,
    "synthetic_icd10": _icd10_valid,
    "synthetic_iban": _iban_valid,
    "synthetic_cusip": _is_valid_cusip,
}


class TestMg1S4RegistryIntegration:
    """Each of the four new MG-1 S4 generators must be addressable
    through `get_default_registry()` AND produce a validator-passing
    value when invoked via the registry-mediated path."""

    @pytest.mark.parametrize("provider", _NEW_PROVIDERS_MG1_S4)
    def test_provider_in_default_registry(self, provider):
        registry = get_default_registry()
        adapter = registry.get_adapter(provider)
        assert adapter is not None
        cap = registry.get_capabilities(provider)
        assert cap.provider == provider
        assert cap.backend_type == "decoy_native"

    @pytest.mark.parametrize("provider", _NEW_PROVIDERS_MG1_S4)
    def test_registry_lookup_returns_validator_passing_value_random(self, provider):
        """End-to-end via the registry, random mode."""
        registry = get_default_registry()
        adapter = registry.get_adapter(provider)
        value = adapter.generate(provider, spec=_spec())
        validator = _VALIDATOR_BY_PROVIDER[provider]
        assert validator(value), (
            f"{provider} via default registry produced non-valid "
            f"output {value!r}"
        )

    @pytest.mark.parametrize("provider", _NEW_PROVIDERS_MG1_S4)
    def test_registry_lookup_returns_validator_passing_value_deterministic(self, provider):
        """End-to-end via the registry, deterministic mode."""
        registry = get_default_registry()
        adapter = registry.get_adapter(provider)
        spec = _spec(deterministic=True, seed=_SEED, namespace="mg1-smoke")
        value = adapter.generate(provider, spec=spec, source_value="seed_input")
        validator = _VALIDATOR_BY_PROVIDER[provider]
        assert validator(value), (
            f"{provider} deterministic-via-registry produced non-valid "
            f"output {value!r}"
        )


class TestTotalProviderCount:
    """The default registry should now bind 9 decoy_native providers
    (5 baseline + 4 from MG-1 S4) on top of the existing Faker
    catalog. Catches a regression where one of the four is silently
    dropped from the registry binding block."""

    def test_all_decoy_native_providers_registered(self):
        registry = get_default_registry()
        expected = {
            # S6 baseline
            "synthetic_ssn",
            "synthetic_ein",
            "synthetic_npi",
            "synthetic_ndc",
            "synthetic_mrn",
            # MG-1 S4 additions
            "synthetic_pan",
            "synthetic_icd10",
            "synthetic_iban",
            "synthetic_cusip",
        }
        for provider in expected:
            cap = registry.get_capabilities(provider)
            assert cap.backend_type == "decoy_native", (
                f"{provider} should be decoy_native; got {cap.backend_type}"
            )
