"""Unit tests for decoy_engine.determinism._derive (derive + derive_index
+ derive_value + Domain + IdentityDomain + DeterminismError).

Covers the S3 spec §Tests "Raw derive", "derive_index", "derive_value",
"DeterminismError shape" blocks. Process-stability + reference vector +
namespace independence live in test_derive_vectors.py and
test_process_stability.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from decoy_engine.determinism import (
    SEED_PROTOCOL_VERSION,
    DeterminismError,
    IdentityDomain,
    derive,
    derive_index,
    derive_value,
)

_SEED = b"\x00\x00\x00\x00\x00\x00\x00\x01"
_NS = "test-namespace"


class TestDeriveShape:
    def test_returns_32_bytes(self) -> None:
        out = derive(_SEED, _NS, b"src")
        assert len(out) == 32
        assert isinstance(out, bytes)

    def test_pure_same_inputs_equal_outputs(self) -> None:
        out_a = derive(_SEED, _NS, b"src")
        out_b = derive(_SEED, _NS, b"src")
        assert out_a == out_b

    def test_different_namespaces_differ(self) -> None:
        a = derive(_SEED, "ns_a", b"src")
        b = derive(_SEED, "ns_b", b"src")
        assert a != b

    def test_different_seeds_differ(self) -> None:
        a = derive(b"\x00" * 8, _NS, b"src")
        b = derive(b"\x00" * 7 + b"\x01", _NS, b"src")
        assert a != b

    def test_different_source_differs(self) -> None:
        a = derive(_SEED, _NS, b"src_a")
        b = derive(_SEED, _NS, b"src_b")
        assert a != b

    def test_empty_source_accepted(self) -> None:
        """M1 resolution: empty source produces deterministic output."""
        a = derive(_SEED, _NS, b"")
        b = derive(_SEED, _NS, b"")
        assert len(a) == 32
        assert a == b

    def test_length_prefix_prevents_collision(self) -> None:
        """Without length prefixing, ("abc","def") and ("abcd","ef")
        would HMAC the same bytes. The 4-byte prefix on namespace +
        source makes the concatenation injective."""
        a = derive(_SEED, "abc", b"def")
        b = derive(_SEED, "abcd", b"ef")
        assert a != b


class TestDeriveValidation:
    def test_seed_wrong_length_short(self) -> None:
        with pytest.raises(DeterminismError) as excinfo:
            derive(b"\x00" * 7, _NS, b"src")
        assert excinfo.value.code == "seed_wrong_length"

    def test_seed_wrong_length_long(self) -> None:
        with pytest.raises(DeterminismError) as excinfo:
            derive(b"\x00" * 9, _NS, b"src")
        assert excinfo.value.code == "seed_wrong_length"

    def test_seed_empty_raises(self) -> None:
        with pytest.raises(DeterminismError) as excinfo:
            derive(b"", _NS, b"src")
        assert excinfo.value.code == "seed_wrong_length"

    def test_namespace_empty_raises(self) -> None:
        with pytest.raises(DeterminismError) as excinfo:
            derive(_SEED, "", b"src")
        assert excinfo.value.code == "namespace_empty"


class TestSeedProtocolVersion:
    def test_constant_is_two(self) -> None:
        """S3 shipped SEED_PROTOCOL_VERSION = 1; the F-series corrections bump
        to 2 (coordinated Faker-seeding + canonicalize-integer fixes)."""
        assert SEED_PROTOCOL_VERSION == 2


class TestDeriveIndex:
    def test_returns_in_range(self) -> None:
        for src in (b"a", b"b", b"c", b"d"):
            idx = derive_index(_SEED, _NS, src, pool_size=100)
            assert 0 <= idx < 100

    def test_pure(self) -> None:
        a = derive_index(_SEED, _NS, b"src", pool_size=1000)
        b = derive_index(_SEED, _NS, b"src", pool_size=1000)
        assert a == b

    def test_pool_size_one_returns_zero(self) -> None:
        """Degenerate case must work: pool_size=1 always returns 0."""
        for src in (b"a", b"b", b"c"):
            assert derive_index(_SEED, _NS, src, pool_size=1) == 0

    def test_overflow_raises(self) -> None:
        with pytest.raises(DeterminismError) as excinfo:
            derive_index(_SEED, _NS, b"src", pool_size=(1 << 56) + 1)
        assert excinfo.value.code == "pool_size_overflow"

    def test_zero_pool_size_raises(self) -> None:
        # QA-7 F11 (2026-06-01): code renamed from pool_size_overflow
        # to pool_size_invalid since a zero/negative pool is an
        # underflow / invalid input, not an overflow.
        with pytest.raises(DeterminismError) as excinfo:
            derive_index(_SEED, _NS, b"src", pool_size=0)
        assert excinfo.value.code == "pool_size_invalid"

    def test_negative_pool_size_raises_invalid(self) -> None:
        with pytest.raises(DeterminismError) as excinfo:
            derive_index(_SEED, _NS, b"src", pool_size=-5)
        assert excinfo.value.code == "pool_size_invalid"

    def test_distribution_approximately_uniform(self) -> None:
        """Sanity: 10_000 distinct sources distribute approximately
        uniformly across pool_size=10 buckets. Coarse bound (not a
        cryptographic claim): no bucket gets <80% or >120% of mean."""
        pool_size = 10
        counts = [0] * pool_size
        for i in range(10_000):
            idx = derive_index(_SEED, _NS, str(i).encode(), pool_size=pool_size)
            counts[idx] += 1
        mean = 10_000 / pool_size  # 1000
        for c in counts:
            assert 0.8 * mean <= c <= 1.2 * mean, f"bucket distribution off: {counts}"


class TestDeriveValueWithIdentityDomain:
    def test_returns_derive_bytes_unchanged(self) -> None:
        """IdentityDomain.from_bytes(b) returns b unchanged, so
        derive_value with IdentityDomain returns derive's output."""
        domain = IdentityDomain()
        raw = derive(_SEED, _NS, b"src")
        result = derive_value(_SEED, _NS, b"src", domain=domain)
        assert result == raw

    def test_pure(self) -> None:
        domain = IdentityDomain()
        a = derive_value(_SEED, _NS, b"src", domain=domain)
        b = derive_value(_SEED, _NS, b"src", domain=domain)
        assert a == b

    def test_calls_domain_from_bytes_with_32_bytes(self) -> None:
        """derive_value passes exactly the 32-byte derive output to
        domain.from_bytes."""

        @dataclass(frozen=True)
        class CapturingDomain:
            def from_bytes(self, b: bytes) -> Any:
                return ("captured", len(b), b)

        domain = CapturingDomain()
        result = derive_value(_SEED, _NS, b"src", domain=domain)
        assert result[0] == "captured"
        assert result[1] == 32

    def test_identity_domain_is_frozen(self) -> None:
        """IdentityDomain is a frozen dataclass: contract is no mutation."""
        from dataclasses import FrozenInstanceError

        domain = IdentityDomain()
        with pytest.raises(FrozenInstanceError):
            domain.something_new = 1  # type: ignore[attr-defined]


class TestDeterminismErrorShape:
    def test_carries_code_and_message(self) -> None:
        e = DeterminismError(code="seed_wrong_length", message="m")
        assert e.code == "seed_wrong_length"
        assert e.message == "m"
        assert "seed_wrong_length" in str(e)
        assert "m" in str(e)

    def test_code_only(self) -> None:
        e = DeterminismError(code="x")
        assert e.code == "x"
        assert e.message == ""
        assert "x" in str(e)

    def test_not_subclass_of_plan_compile_error(self) -> None:
        """Per S3 spec §3.5: DeterminismError is NOT a PlanCompileError."""
        from decoy_engine.plan._errors import PlanCompileError

        e = DeterminismError(code="x")
        assert not isinstance(e, PlanCompileError)
