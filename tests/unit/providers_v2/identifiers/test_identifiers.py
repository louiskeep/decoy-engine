"""Per-identifier tests for the 5 S6 swap targets.

Covers the §Tests block from the S6 spec: format regex, invalid-pattern
(blocklist), Domain purity, canonicalization parity, batch-deterministic
rejection, capability matrix shape, and the registry-layer poolable=False
enforcement (M-NEW1 resolution).
"""

from __future__ import annotations

import re
import subprocess
import sys

import numpy as np
import pytest

from decoy_engine.determinism import derive_value
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.providers_v2 import ProviderError, ProviderSpec, get_default_registry
from decoy_engine.providers_v2.identifiers import (
    EinAdapter,
    EinDomain,
    EinValidator,
    IdentifierError,
    MrnAdapter,
    MrnDomain,
    MrnValidator,
    NdcAdapter,
    NdcDomain,
    NdcValidator,
    NpiAdapter,
    NpiDomain,
    NpiValidator,
    SsnAdapter,
    SsnDomain,
    SsnValidator,
)

_SEED = b"\x00\x00\x00\x00\x00\x00\x00\x2a"  # 42
_NS = "customer_identity"
_SEED_HEX = _SEED.hex()  # used by subprocess cross-process tests

# 5 (adapter, domain, validator, provider_name, format_regex) tuples
_IDENTIFIERS = [
    (SsnAdapter, SsnDomain, SsnValidator, "synthetic_ssn", r"^\d{3}-\d{2}-\d{4}$"),
    (EinAdapter, EinDomain, EinValidator, "synthetic_ein", r"^\d{2}-\d{7}$"),
    (NpiAdapter, NpiDomain, NpiValidator, "synthetic_npi", r"^\d{10}$"),
    (NdcAdapter, NdcDomain, NdcValidator, "synthetic_ndc", r"^\d{4,5}-\d{3,4}-\d{1,2}$"),
    (MrnAdapter, MrnDomain, MrnValidator, "synthetic_mrn", r"^[A-Za-z]*\d+$"),
]


@pytest.mark.parametrize(
    ("adapter_cls", "domain_cls", "validator_cls", "provider", "regex"), _IDENTIFIERS
)
class TestFormatPerIdentifier:
    """Format regex: 1000 non-deterministic outputs all match the format."""

    def test_generate_batch_format_compliance(
        self, adapter_cls, domain_cls, validator_cls, provider, regex
    ) -> None:
        spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None)
        outputs = adapter_cls().generate_batch(provider, spec=spec, count=100)
        pattern = re.compile(regex)
        for out in outputs:
            assert pattern.match(out), f"{provider}: output {out!r} does not match {regex!r}"


class TestSsnBlocklist:
    """SSA POMS rules: area not 000/666/900-999; group not 00; serial not 0000."""

    def test_10k_samples_no_blocklisted_areas(self) -> None:
        for _ in range(10_000):
            ssn = SsnAdapter().generate(
                "synthetic_ssn",
                spec=ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None),
            )
            assert SsnValidator.is_valid(ssn), f"blocklist leak: {ssn}"


class TestEinPrefixList:
    def test_10k_samples_use_irs_prefix(self) -> None:
        for _ in range(1000):
            ein = EinAdapter().generate(
                "synthetic_ein",
                spec=ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None),
            )
            assert EinValidator.is_valid(ein), f"invalid EIN prefix: {ein}"


class TestNpiLuhnCheck:
    def test_10k_samples_pass_luhn(self) -> None:
        for _ in range(10_000):
            npi = NpiAdapter().generate(
                "synthetic_npi",
                spec=ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None),
            )
            assert NpiValidator.is_valid(npi), f"NPI failed Luhn: {npi}"


class TestNdcSegmentLayouts:
    def test_default_layout_validates(self) -> None:
        for _ in range(100):
            ndc = NdcAdapter().generate(
                "synthetic_ndc",
                spec=ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None),
            )
            assert NdcValidator.is_valid(ndc), f"invalid NDC: {ndc}"


class TestMrnConfigurability:
    def test_default_8_digit_no_leading_zero(self) -> None:
        for _ in range(100):
            mrn = MrnAdapter().generate(
                "synthetic_mrn",
                spec=ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None),
            )
            assert len(mrn) == 8 and mrn[0] != "0" and mrn.isdigit()

    def test_alpha_prefix_override(self) -> None:
        spec = ProviderSpec(
            locale="en_US",
            deterministic=False,
            namespace=None,
            seed=None,
            extra={"mrn_alpha_prefix": "MRN"},
        )
        out = MrnAdapter().generate("synthetic_mrn", spec=spec)
        assert out.startswith("MRN")


@pytest.mark.parametrize(
    ("adapter_cls", "domain_cls", "validator_cls", "provider", "regex"), _IDENTIFIERS
)
class TestDeterminismContract:
    """Per S6 spec §5: same source -> same output across 100 calls (Domain purity)."""

    def test_domain_from_bytes_pure_100_calls(
        self, adapter_cls, domain_cls, validator_cls, provider, regex
    ) -> None:
        b = b"\x42" * 32
        domain = domain_cls()
        outputs = [domain.from_bytes(b) for _ in range(100)]
        assert len(set(outputs)) == 1

    def test_adapter_generate_same_source_same_output(
        self, adapter_cls, domain_cls, validator_cls, provider, regex
    ) -> None:
        spec = ProviderSpec(locale="en_US", deterministic=True, namespace=_NS, seed=_SEED)
        out_a = adapter_cls().generate(provider, spec=spec, source_value=42)
        out_b = adapter_cls().generate(provider, spec=spec, source_value=42)
        assert out_a == out_b


@pytest.mark.parametrize(
    ("adapter_cls", "domain_cls", "validator_cls", "provider", "regex"), _IDENTIFIERS
)
class TestCanonicalizationParity:
    """Per H2 + §3.5: adapter output equals derive_value(_canonicalize_source(value), domain)."""

    def test_int_source_uses_int_branch(
        self, adapter_cls, domain_cls, validator_cls, provider, regex
    ) -> None:
        spec = ProviderSpec(locale="en_US", deterministic=True, namespace=_NS, seed=_SEED)
        out_adapter = adapter_cls().generate(provider, spec=spec, source_value=42)
        # Hand-compute via the shipped helper.
        canonical = _canonicalize_source(42)
        out_hand = derive_value(seed=_SEED, namespace=_NS, source=canonical, domain=domain_cls())
        assert out_adapter == out_hand

    def test_canonical_int_parity_python_and_numpy(
        self, adapter_cls, domain_cls, validator_cls, provider, regex
    ) -> None:
        """Python int and numpy.int64 canonicalize identically via numbers.Integral.

        The S5 F-series corrections replaced the isinstance(value, int) dispatch
        with isinstance(value, numbers.Integral), which catches numpy integer
        scalars (pd.Series.iloc[i] returns numpy.int64, not Python int). This
        test asserts the full parity contract per S6 spec §Tests.
        """
        assert _canonicalize_source(42) == _canonicalize_source(np.int64(42))


@pytest.mark.parametrize(
    ("adapter_cls", "domain_cls", "validator_cls", "provider", "regex"), _IDENTIFIERS
)
class TestBatchDeterministicRejection:
    """Per M2 + §3 template: generate_batch with deterministic=True raises."""

    def test_batch_deterministic_raises(
        self, adapter_cls, domain_cls, validator_cls, provider, regex
    ) -> None:
        spec = ProviderSpec(locale="en_US", deterministic=True, namespace=_NS, seed=_SEED)
        with pytest.raises(ProviderError) as excinfo:
            adapter_cls().generate_batch(provider, spec=spec, count=5)
        assert excinfo.value.code == "batch_deterministic_unsupported"


@pytest.mark.parametrize(
    ("adapter_cls", "domain_cls", "validator_cls", "provider", "regex"), _IDENTIFIERS
)
class TestCapabilityMatrix:
    """All 5 S6 swap targets declare poolable=False, decoy_native backend_type."""

    def test_poolable_false(self, adapter_cls, domain_cls, validator_cls, provider, regex) -> None:
        cap = adapter_cls().capability_matrix(provider)
        assert cap.poolable is False

    def test_backend_type_decoy_native(
        self, adapter_cls, domain_cls, validator_cls, provider, regex
    ) -> None:
        cap = adapter_cls().capability_matrix(provider)
        assert cap.backend_type == "decoy_native"

    def test_supports_deterministic_true(
        self, adapter_cls, domain_cls, validator_cls, provider, regex
    ) -> None:
        cap = adapter_cls().capability_matrix(provider)
        assert cap.supports_deterministic is True


@pytest.mark.parametrize(
    "provider",
    ["synthetic_ssn", "synthetic_ein", "synthetic_npi", "synthetic_ndc", "synthetic_mrn"],
)
class TestRegistrySwapPoolableEnforcement:
    """M-NEW1 resolution: registry-layer test enforces poolable=False
    for the 5 swap targets (no new method on PoolAdapter)."""

    def test_registry_capability_poolable_false(self, provider: str) -> None:
        cap = get_default_registry().get_capabilities(provider)
        assert cap.poolable is False

    def test_registry_capability_backend_type_decoy_native(self, provider: str) -> None:
        cap = get_default_registry().get_capabilities(provider)
        assert cap.backend_type == "decoy_native"


class TestUnsupportedLocale:
    """M4 from S6 review: locale gate test for all 5 identifiers."""

    def test_ssn_non_en_us_raises(self) -> None:
        spec = ProviderSpec(locale="fr_FR", deterministic=False, namespace=None, seed=None)
        with pytest.raises(ProviderError) as excinfo:
            SsnAdapter().generate("synthetic_ssn", spec=spec)
        assert excinfo.value.code == "unsupported_locale"

    def test_ein_non_en_us_raises(self) -> None:
        spec = ProviderSpec(locale="fr_FR", deterministic=False, namespace=None, seed=None)
        with pytest.raises(ProviderError) as excinfo:
            EinAdapter().generate("synthetic_ein", spec=spec)
        assert excinfo.value.code == "unsupported_locale"

    def test_npi_non_en_us_raises(self) -> None:
        spec = ProviderSpec(locale="fr_FR", deterministic=False, namespace=None, seed=None)
        with pytest.raises(ProviderError) as excinfo:
            NpiAdapter().generate("synthetic_npi", spec=spec)
        assert excinfo.value.code == "unsupported_locale"

    def test_ndc_non_en_us_raises(self) -> None:
        spec = ProviderSpec(locale="fr_FR", deterministic=False, namespace=None, seed=None)
        with pytest.raises(ProviderError) as excinfo:
            NdcAdapter().generate("synthetic_ndc", spec=spec)
        assert excinfo.value.code == "unsupported_locale"

    def test_mrn_non_en_us_passes(self) -> None:
        """MRN is locale-agnostic (per-site format; no SSA-style restriction).

        supported_locales=('en_US',) in capability_matrix is an advisory
        declaration; MRN's generate_random() intentionally ignores locale.
        Non-en_US inputs produce valid MRNs without raising.
        """
        spec = ProviderSpec(locale="fr_FR", deterministic=False, namespace=None, seed=None)
        out = MrnAdapter().generate("synthetic_mrn", spec=spec)
        assert MrnValidator.is_valid(out)


class TestCrossProcessStability:
    """H2 from S6 review: subprocess and parent produce byte-identical outputs for fixed seed."""

    @pytest.mark.parametrize(
        ("adapter_name", "provider"),
        [
            ("SsnAdapter", "synthetic_ssn"),
            ("EinAdapter", "synthetic_ein"),
            ("NpiAdapter", "synthetic_npi"),
            ("NdcAdapter", "synthetic_ndc"),
            ("MrnAdapter", "synthetic_mrn"),
        ],
    )
    def test_subprocess_matches_inprocess(self, adapter_name: str, provider: str) -> None:
        _adapter_cls_map = {
            "SsnAdapter": SsnAdapter,
            "EinAdapter": EinAdapter,
            "NpiAdapter": NpiAdapter,
            "NdcAdapter": NdcAdapter,
            "MrnAdapter": MrnAdapter,
        }
        spec = ProviderSpec(locale="en_US", deterministic=True, namespace=_NS, seed=_SEED)
        expected = _adapter_cls_map[adapter_name]().generate(provider, spec=spec, source_value=42)
        code = (
            f"from decoy_engine.providers_v2.identifiers import {adapter_name}; "
            f"from decoy_engine.providers_v2 import ProviderSpec; "
            f"spec = ProviderSpec(locale='en_US', deterministic=True, "
            f"namespace={_NS!r}, seed=bytes.fromhex({_SEED_HEX!r})); "
            f"print({adapter_name}().generate({provider!r}, spec=spec, source_value=42))"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == expected


class TestNoSourceLeak:
    """H3 from S6 review: deterministic output must never equal source value.

    Tests 100,000 distinct integer source values per identifier. Any failure
    indicates a logic bug (e.g. the adapter returned source_value directly
    instead of routing through derive_value + domain).
    """

    @pytest.mark.parametrize(
        ("adapter_cls", "provider"),
        [
            (SsnAdapter, "synthetic_ssn"),
            (EinAdapter, "synthetic_ein"),
            (NpiAdapter, "synthetic_npi"),
            (NdcAdapter, "synthetic_ndc"),
            (MrnAdapter, "synthetic_mrn"),
        ],
    )
    def test_no_source_leak_100k_int_source(self, adapter_cls: type, provider: str) -> None:
        spec = ProviderSpec(locale="en_US", deterministic=True, namespace=_NS, seed=_SEED)
        blocklist_exhausted = 0
        for i in range(100_000):
            try:
                out = adapter_cls().generate(provider, spec=spec, source_value=i)
            except IdentifierError as exc:
                if exc.code == "blocklist_exhausted":
                    # SsnDomain's 4-offset rehash can exhaust (~1.5e-4 per row).
                    # This is the S6 H1 forward risk: S9 must handle IdentifierError
                    # at execution time (see dennis-engine-v2-s6 review, Session 24).
                    blocklist_exhausted += 1
                    continue
                raise
            assert out != str(i), f"{provider}: output equals str(source) at i={i}"
        # Sanity-check the exhaustion rate stays within the documented ~1.5e-4 bound.
        assert blocklist_exhausted < 500, (
            f"{provider}: {blocklist_exhausted}/100000 blocklist_exhausted - "
            "rate far exceeds expected ~15/100000; blocklist or domain logic is buggy."
        )
