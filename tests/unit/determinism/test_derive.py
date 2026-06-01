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


class TestQa10F4DeriveContext:
    """QA-10 F4 (2026-06-01, HIGH perf): DeriveContext amortises the
    HKDF cost across per-row calls. Pre-fix every `derive(seed, ns, src)`
    call recomputed `hkdf_sha256(ikm=seed, info=ns)` (2x HMAC-SHA256).
    The HKDF output depends only on (seed, namespace) which are
    constant for every row in a column; the per-row work is the final
    HMAC-SHA256 of the source-bytes payload.

    DeriveContext.for_column() builds the key once; .derive_source()
    runs only the per-row HMAC. Output is byte-identical to the
    scalar derive() function for the same inputs."""

    def test_derive_context_output_matches_scalar_derive(self):
        """Byte-parity contract: ctx.derive_source(ns, src) ==
        derive(seed, ns, src) for the same inputs."""
        from decoy_engine.determinism._derive import DeriveContext, derive

        seed = b"\x00" * 8
        ns = "test_namespace"
        ctx = DeriveContext.for_column(seed, ns)
        for src in [b"row-0", b"row-1", b"row-2", b"", b"longer-source-bytes"]:
            assert ctx.derive_source(ns, src) == derive(seed, ns, src), (
                f"DeriveContext.derive_source diverged from scalar derive "
                f"on source={src!r}; byte-parity contract violated"
            )

    def test_derive_context_validates_seed_length(self):
        """for_column raises on wrong-length seed (same contract as
        scalar derive)."""
        from decoy_engine.determinism._derive import DeriveContext

        with pytest.raises(DeterminismError) as exc:
            DeriveContext.for_column(b"\x00" * 7, "ns")
        assert exc.value.code == "seed_wrong_length"

    def test_derive_context_validates_namespace_empty(self):
        from decoy_engine.determinism._derive import DeriveContext

        with pytest.raises(DeterminismError) as exc:
            DeriveContext.for_column(b"\x00" * 8, "")
        assert exc.value.code == "namespace_empty"

    def test_derive_context_amortises_hkdf(self):
        """Smoke check: 1000 per-row calls should take meaningfully
        less time with the context than 1000 scalar derive calls.
        Not a perf budget cell (that's PV-2 scope); a sanity check
        that the F4 optimization is wired."""
        import time

        from decoy_engine.determinism._derive import DeriveContext, derive

        seed = b"\x00" * 8
        ns = "perf_check_namespace"
        rows = [i.to_bytes(4, "big") for i in range(1_000)]

        # Scalar (recomputes HKDF every call).
        start = time.perf_counter()
        for src in rows:
            derive(seed, ns, src)
        scalar_s = time.perf_counter() - start

        # Context (HKDF computed once).
        start = time.perf_counter()
        ctx = DeriveContext.for_column(seed, ns)
        for src in rows:
            ctx.derive_source(ns, src)
        ctx_s = time.perf_counter() - start

        # Context should be measurably faster; assert at least 1.5x
        # speedup on the dev box. If the perf optimization regresses
        # (HKDF re-computed inside derive_source), this cell catches it.
        assert ctx_s < scalar_s, (
            f"DeriveContext ({ctx_s*1000:.0f}ms) not faster than "
            f"scalar derive ({scalar_s*1000:.0f}ms); QA-10 F4 may "
            "have regressed."
        )
