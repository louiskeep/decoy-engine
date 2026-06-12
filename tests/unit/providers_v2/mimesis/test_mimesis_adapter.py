"""engine-v2 S7: MimesisAdapter + 7-check parity suite tests.

The whole module is skipped when the optional `mimesis` dep is absent
(the optional-dep / registry-stays-24 behavior is tested separately in
test_optional_dep.py, which does not import mimesis).
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytest.importorskip("mimesis")

from decoy_engine.generation.pool._builder import PoolBuilder
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._pool_adapter import PoolAdapter
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.providers_v2._faker_adapter import FakerAdapter
from decoy_engine.providers_v2.identifiers import (
    deterministic_namespace_completeness,
)
from decoy_engine.providers_v2.mimesis import (
    ADOPTED_MIMESIS_PROVIDERS,
    MIMESIS_CANDIDATES,
    MimesisAdapter,
    is_adoptable,
    mimesis_capability,
    run_parity_suite,
)
from decoy_engine.providers_v2.mimesis._parity import (
    ParityCheckResult,
    check_determinism,
    check_distribution,
    check_dtype,
    check_format,
    check_length,
    check_null,
)

SEED = (0xABCDEF12).to_bytes(8, "big")


def _nd(locale: str = "en_US") -> ProviderSpec:
    return ProviderSpec(locale=locale, deterministic=False, namespace=None, seed=None)


def _seeded(locale: str = "en_US", seed: bytes = SEED) -> ProviderSpec:
    return ProviderSpec(locale=locale, deterministic=False, namespace=None, seed=seed)


class TestMimesisAdapterBasics:
    def test_generate_returns_value(self) -> None:
        out = MimesisAdapter().generate("person_email", spec=_nd())
        assert isinstance(out, str) and "@" in out

    def test_capability_poolable_and_nondeterministic(self) -> None:
        cap = MimesisAdapter().capability_matrix("person_first_name")
        assert cap.poolable is True
        assert cap.supports_deterministic is False
        assert cap.backend_type == "mimesis"

    def test_unknown_provider_generate_raises(self) -> None:
        with pytest.raises(ProviderError) as exc:
            MimesisAdapter().generate("not_a_provider", spec=_nd())
        assert exc.value.code == "unknown_provider"

    def test_candidate_set_is_eleven(self) -> None:
        assert len(MIMESIS_CANDIDATES) == 11


class TestDeterministicMode:
    def test_direct_deterministic_rejected(self) -> None:
        with pytest.raises(ProviderError) as exc:
            MimesisAdapter().generate(
                "person_first_name",
                spec=ProviderSpec(locale="en_US", deterministic=True, namespace="ns", seed=SEED),
            )
        assert exc.value.code == "capability_violation"

    def test_generate_batch_deterministic_rejected(self) -> None:
        with pytest.raises(ProviderError) as exc:
            MimesisAdapter().generate_batch(
                "person_first_name",
                spec=ProviderSpec(locale="en_US", deterministic=True, namespace="ns", seed=SEED),
                count=4,
            )
        assert exc.value.code == "capability_violation"

    @pytest.mark.parametrize("provider", sorted(MIMESIS_CANDIDATES))
    def test_seeded_batch_reproducible_in_process(self, provider: str) -> None:
        a = MimesisAdapter().generate_batch(provider, spec=_seeded(), count=32)
        b = MimesisAdapter().generate_batch(provider, spec=_seeded(), count=32)
        assert list(a) == list(b)

    def test_seeded_batch_unseeded_is_random(self) -> None:
        a = MimesisAdapter().generate_batch("person_first_name", spec=_nd(), count=32)
        b = MimesisAdapter().generate_batch("person_first_name", spec=_nd(), count=32)
        assert list(a) != list(b)  # astronomically unlikely to collide

    @pytest.mark.parametrize("provider", sorted(MIMESIS_CANDIDATES))
    def test_seeded_batch_reproducible_cross_process(self, provider: str) -> None:
        # str() each value so the comparison is dtype-agnostic: person_dob emits
        # datetime.date (not JSON-serializable), and the str repr is sufficient
        # to prove byte-identical cross-process determinism (MEDIUM-S7-DOB-DTYPE-1).
        script = (
            "import json;"
            "from decoy_engine.providers_v2._adapter import ProviderSpec;"
            "from decoy_engine.providers_v2.mimesis import MimesisAdapter;"
            "s=ProviderSpec(locale='en_US',deterministic=False,namespace=None,seed=(0xABCDEF12).to_bytes(8,'big'));"
            f"print(json.dumps([str(v) for v in MimesisAdapter().generate_batch({provider!r},spec=s,count=32)]))"
        )
        result = subprocess.run(  # noqa: S603 -- args are test literals, not untrusted input
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        )
        child = json.loads(result.stdout.strip())
        parent = [
            str(v) for v in MimesisAdapter().generate_batch(provider, spec=_seeded(), count=32)
        ]
        assert child == parent

    def _pool_adapter(self, provider: str) -> PoolAdapter:
        madapter = MimesisAdapter(fallback=FakerAdapter(locale="en_US"))
        reg = get_default_registry().override(provider, madapter, mimesis_capability(provider))
        return PoolAdapter(madapter, builder=PoolBuilder(reg), cache=PoolCache())

    def test_pool_routed_same_source_same_value(self) -> None:
        pa = self._pool_adapter("person_first_name")
        spec = ProviderSpec(
            locale="en_US", deterministic=True, namespace="ns1", seed=SEED, extra={"pool_size": 256}
        )
        v1 = pa.generate("person_first_name", spec=spec, source_value=b"alice")
        v2 = pa.generate("person_first_name", spec=spec, source_value=b"alice")
        assert v1 == v2

    def test_pool_routed_different_source_differs(self) -> None:
        pa = self._pool_adapter("person_first_name")
        spec = ProviderSpec(
            locale="en_US",
            deterministic=True,
            namespace="ns1",
            seed=SEED,
            extra={"pool_size": 4096},
        )
        vals = {
            pa.generate("person_first_name", spec=spec, source_value=f"s{i}".encode())
            for i in range(20)
        }
        assert len(vals) > 1

    def test_pool_routed_different_namespace_differs(self) -> None:
        pa = self._pool_adapter("person_first_name")
        common = dict(locale="en_US", deterministic=True, seed=SEED, extra={"pool_size": 4096})
        v_ns1 = pa.generate(
            "person_first_name", spec=ProviderSpec(namespace="ns1", **common), source_value=b"alice"
        )
        v_ns2 = pa.generate(
            "person_first_name", spec=ProviderSpec(namespace="ns2", **common), source_value=b"alice"
        )
        # Different namespace -> different pool seed -> (almost surely) different value.
        assert v_ns1 != v_ns2

    def test_pooladapter_boosts_supports_deterministic(self) -> None:
        pa = self._pool_adapter("person_first_name")
        assert pa.capability_matrix("person_first_name").supports_deterministic is True
        # Unwrapped adapter stays False.
        assert (
            MimesisAdapter().capability_matrix("person_first_name").supports_deterministic is False
        )

    def test_pool_routed_person_dob_date_dtype(self) -> None:
        # MEDIUM-S7-DOB-DTYPE-1: person_dob is the only non-str candidate
        # (datetime.date). Exercise its pool-routed deterministic path so a
        # date-typed pool/bundle entry can't silently break a str-assuming consumer.
        import datetime

        pa = self._pool_adapter("person_dob")
        spec = ProviderSpec(
            locale="en_US", deterministic=True, namespace="ns1", seed=SEED, extra={"pool_size": 256}
        )
        v1 = pa.generate("person_dob", spec=spec, source_value=b"alice")
        v2 = pa.generate("person_dob", spec=spec, source_value=b"alice")
        assert isinstance(v1, datetime.date)
        assert v1 == v2


class TestLocaleFallback:
    def test_supported_locale_uses_mimesis_no_warning(self) -> None:
        m = MimesisAdapter(fallback=FakerAdapter(locale="en_US"))
        out = m.generate("person_first_name", spec=_nd("en_US"))
        assert isinstance(out, str) and out
        assert m.warnings == ()

    def test_unsupported_locale_falls_back_with_warning(self) -> None:
        m = MimesisAdapter(fallback=FakerAdapter(locale="en_US"))
        out = m.generate("person_first_name", spec=_nd("th_TH"))
        assert isinstance(out, str) and out
        assert [w.code for w in m.warnings] == ["mimesis_locale_fallback"]
        assert m.warnings[0].detail["requested_locale"] == "th_TH"

    def test_no_fallback_configured_raises(self) -> None:
        with pytest.raises(ProviderError) as exc:
            MimesisAdapter(fallback=None).generate("person_first_name", spec=_nd("th_TH"))
        assert exc.value.code == "unsupported_locale"

    def test_fallback_batch_reproducible(self) -> None:
        # Fallback is a seeded Faker call; determinism holds on the fallback path.
        m1 = MimesisAdapter(fallback=FakerAdapter(locale="en_US"))
        m2 = MimesisAdapter(fallback=FakerAdapter(locale="en_US"))
        b1 = m1.generate_batch("person_first_name", spec=_seeded("th_TH"), count=16)
        b2 = m2.generate_batch("person_first_name", spec=_seeded("th_TH"), count=16)
        assert list(b1) == list(b2)


class TestParitySuite:
    @pytest.mark.parametrize("provider", sorted(MIMESIS_CANDIDATES))
    def test_returns_seven_results(self, provider: str) -> None:
        # MEDIUM-S7-PARITY-COVERAGE-1: run the full parity suite for every one of
        # the 11 candidates so a future adoption cannot bypass the gate.
        results = run_parity_suite(provider, n=100)
        assert len(results) == 7
        assert {r.check for r in results} == {
            "dtype",
            "null",
            "locale",
            "length",
            "format",
            "determinism",
            "distribution",
        }
        # The determinism check (the hard gate) must pass for every candidate.
        determinism = next(r for r in results if r.check == "determinism")
        assert determinism.passed is True

    def test_results_carry_benchmark_ratio(self) -> None:
        results = run_parity_suite("person_first_name", n=200)
        assert all(r.benchmark_ratio is not None for r in results)

    def test_determinism_check_passes_for_real_provider(self) -> None:
        passed, _ = check_determinism("person_first_name", "en_US")
        assert passed is True

    # Each check fails correctly when artificially broken (crafted inputs).
    def test_check_dtype_detects_mismatch(self) -> None:
        passed, _ = check_dtype(["a", "b"], [1, 2])
        assert passed is False

    def test_check_null_detects_mismatch(self) -> None:
        passed, _ = check_null(["a", None], ["a", "b"])
        assert passed is False

    def test_check_length_detects_outlier(self) -> None:
        passed, _ = check_length(["x" * 100] * 10, ["xx"] * 10)
        assert passed is False

    def test_check_format_detects_bad(self) -> None:
        passed, _ = check_format("person_email", ["not-an-email", "also bad"])
        assert passed is False

    def test_check_format_passes_when_no_constraint(self) -> None:
        passed, detail = check_format("person_first_name", ["Anything", "Goes"])
        assert passed is True
        assert detail["format_regex"] is None

    def test_check_distribution_flags_narrow_pool(self) -> None:
        passed, _ = check_distribution(["same"] * 100, [str(i) for i in range(100)])
        assert passed is False

    def test_is_adoptable_requires_speed(self) -> None:
        fast = [
            ParityCheckResult("p", c, True, {}, 0.05)
            for c in ("dtype", "null", "locale", "length", "format", "determinism", "distribution")
        ]
        slow = [
            ParityCheckResult("p", c, True, {}, 0.90)
            for c in ("dtype", "null", "locale", "length", "format", "determinism", "distribution")
        ]
        assert is_adoptable(fast) is True
        assert is_adoptable(slow) is False

    def test_is_adoptable_requires_gating_checks(self) -> None:
        broken = [
            ParityCheckResult("p", c, c != "determinism", {}, 0.05)
            for c in ("dtype", "null", "locale", "length", "format", "determinism", "distribution")
        ]
        assert is_adoptable(broken) is False  # determinism failed


# Adoption evaluation outcome (2026-06-12, mimesis 19.1.0): the five person
# string providers cleared the gate (checks 1-6 pass, ratios 0.018-0.060).
# Failers: address_state 0.37 / address_zip 0.82 / person_dob 0.20-0.25 on
# speed; address_city, address_street, person_phone on length parity.
# person_first_name fails only advisory check 7 with MORE distinct values
# than Faker (3103 vs 656) -- richer pool, adopted per the is_adoptable
# predicate. Full results: docs/mimesis-adoption-2026-06-12.md.
_EVALUATED_ADOPTED = frozenset(
    {
        "person_name",
        "person_first_name",
        "person_last_name",
        "person_full_name",
        "person_email",
    }
)


class TestAdoptionMatrix:
    def test_adopted_set_matches_2026_06_12_evaluation(self) -> None:
        assert _EVALUATED_ADOPTED == ADOPTED_MIMESIS_PROVIDERS

    def test_adopted_is_subset_of_candidates(self) -> None:
        assert ADOPTED_MIMESIS_PROVIDERS <= MIMESIS_CANDIDATES

    def test_adopted_providers_bind_to_mimesis_adapter(self) -> None:
        registry = get_default_registry()
        for provider in ADOPTED_MIMESIS_PROVIDERS:
            adapter = registry.get_adapter(provider)
            assert isinstance(adapter, MimesisAdapter), provider

    def test_non_adopted_candidates_stay_on_faker(self) -> None:
        registry = get_default_registry()
        for provider in MIMESIS_CANDIDATES - ADOPTED_MIMESIS_PROVIDERS:
            adapter = registry.get_adapter(provider)
            assert isinstance(adapter, FakerAdapter), provider


class TestAdoptionDriftTripwire:
    """Gating checks 1-6 must keep passing for every adopted provider.

    A mimesis (or Faker) upgrade that breaks behavior parity for an adopted
    provider must fail CI, not silently ship divergent pools. Samples are
    SEEDED so the verdict is deterministic per dependency version: only a
    real behavior change in either backend can flip it, never sampling
    noise (person_full_name max-length parity sits near the 20% tolerance,
    so unseeded samples flake). The benchmark ratio is deliberately not
    asserted: CI timing is noisy and speed regressions do not corrupt
    output. Re-evaluate ratios manually on dependency bumps (see
    docs/mimesis-adoption-2026-06-12.md)."""

    @pytest.mark.parametrize("provider", sorted(_EVALUATED_ADOPTED))
    def test_gating_checks_still_pass(self, provider: str) -> None:
        from decoy_engine.providers_v2.mimesis._parity import check_locale

        spec = _seeded()
        m = list(MimesisAdapter().generate_batch(provider, spec=spec, count=2_000))
        f = list(FakerAdapter().generate_batch(provider, spec=spec, count=2_000))
        verdicts = {
            "dtype": check_dtype(m, f),
            "null": check_null(m, f),
            "locale": check_locale("en_US"),
            "length": check_length(m, f),
            "format": check_format(provider, m),
            "determinism": check_determinism(provider, "en_US"),
        }
        failed = {c: d for c, (ok, d) in verdicts.items() if not ok}
        assert not failed, f"{provider}: gating parity checks now fail: {failed}"

    def test_candidates_match_spec(self) -> None:
        assert (
            frozenset(
                {
                    "person_name",
                    "person_first_name",
                    "person_last_name",
                    "person_full_name",
                    "person_email",
                    "person_phone",
                    "person_dob",
                    "address_street",
                    "address_city",
                    "address_state",
                    "address_zip",
                }
            )
            == MIMESIS_CANDIDATES
        )


class TestRow9MimesisAdopted:
    """Row 9 (deterministic_namespace_completeness) is inherited from S6; S7
    adds a Mimesis-adopted-provider exercise of it (S7 spec §Tests)."""

    def test_deterministic_mimesis_provider_without_namespace_raises(self) -> None:
        config = {
            "tables": [
                {
                    "name": "patients",
                    "columns": [
                        {"name": "first", "provider": "person_first_name", "deterministic": True}
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError) as exc:
            deterministic_namespace_completeness(config)
        assert exc.value.code == "deterministic_namespace_missing"

    def test_deterministic_mimesis_provider_with_namespace_passes(self) -> None:
        config = {
            "tables": [
                {
                    "name": "patients",
                    "columns": [
                        {
                            "name": "first",
                            "provider": "person_first_name",
                            "deterministic": True,
                            "namespace": "people",
                        }
                    ],
                }
            ]
        }
        deterministic_namespace_completeness(config)  # no raise
