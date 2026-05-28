"""PoolAdapter wrapping pattern tests (S5 spec §10 + PO PQ1 call).

PoolAdapter is the first concrete BackendAdapter that declares
supports_deterministic=True for the poolable subset. The boost is a
runtime view via model_copy; the live registry stays False.
"""

from __future__ import annotations

import pytest

from decoy_engine.generation.pool import (
    PoolAdapter,
    PoolBuilder,
    PoolCache,
)
from decoy_engine.providers_v2 import (
    ProviderError,
    ProviderSpec,
    get_default_registry,
)


def _adapter() -> PoolAdapter:
    registry = get_default_registry()
    inner = registry.get_adapter("person_email")
    return PoolAdapter(
        wrapped=inner,
        builder=PoolBuilder(registry),
        cache=PoolCache(max_bytes=10_000_000),
    )


class TestCapabilityBoosting:
    def test_poolable_provider_supports_deterministic_true(self) -> None:
        """PQ1: PoolAdapter wraps another adapter and flips
        supports_deterministic via model_copy for poolable providers."""
        cap = _adapter().capability_matrix("person_email")
        assert cap.supports_deterministic is True

    def test_poolable_provider_backend_type_is_pool(self) -> None:
        cap = _adapter().capability_matrix("person_email")
        assert cap.backend_type == "pool"

    def test_non_poolable_provider_passes_through_unchanged(self) -> None:
        """address_full declares poolable=False; PoolAdapter does NOT
        boost its capability matrix."""
        cap = _adapter().capability_matrix("address_full")
        assert cap.supports_deterministic is False
        assert cap.backend_type == "faker"  # unchanged

    def test_live_registry_view_stays_false(self) -> None:
        """PoolAdapter's view is the source of truth WHEN routed through
        the wrapping pattern. The live registry view stays as the source
        of truth WHEN routed directly through FakerAdapter."""
        registry = get_default_registry()
        live_cap = registry.get_capabilities("person_email")
        assert live_cap.supports_deterministic is False
        assert live_cap.backend_type == "faker"


class TestDeterministicGenerate:
    def test_same_source_returns_byte_identical_output(self) -> None:
        """Deferred from S4 H2: deterministic-Faker assertion lands here."""
        adapter = _adapter()
        spec = ProviderSpec(
            locale="en_US",
            deterministic=True,
            namespace="customer_identity",
            seed=b"\x00\x00\x00\x00\x00\x00\x00\x2a",
        )
        out_a = adapter.generate("person_email", spec=spec, source_value=b"alice@example.com")
        out_b = adapter.generate("person_email", spec=spec, source_value=b"alice@example.com")
        assert out_a == out_b

    def test_different_sources_produce_different_output(self) -> None:
        adapter = _adapter()
        spec = ProviderSpec(
            locale="en_US",
            deterministic=True,
            namespace="customer_identity",
            seed=b"\x00\x00\x00\x00\x00\x00\x00\x2a",
        )
        out_a = adapter.generate("person_email", spec=spec, source_value=b"a")
        out_b = adapter.generate("person_email", spec=spec, source_value=b"b")
        # Almost certainly different (pool of 10k; collision is unlikely
        # for two specific values).
        assert out_a != out_b

    def test_deterministic_without_source_value_raises(self) -> None:
        adapter = _adapter()
        spec = ProviderSpec(
            locale="en_US",
            deterministic=True,
            namespace="customer_identity",
            seed=b"\x00" * 8,
        )
        with pytest.raises(ProviderError) as excinfo:
            adapter.generate("person_email", spec=spec, source_value=None)
        assert excinfo.value.code == "deterministic_requires_source_value"


class TestCacheBeforeBuild:
    """F1: the cache is consulted (via PoolBuilder.identity_for) BEFORE
    building, so N identical-spec deterministic generates build exactly one
    pool. The prior code built first and discarded the result on a cache hit,
    so every deterministic generate paid a full rebuild and the >90%
    cache-hit performance gate was structurally unreachable."""

    def test_repeated_generate_builds_pool_once(self) -> None:
        registry = get_default_registry()
        builder = PoolBuilder(registry)
        calls = {"n": 0}
        real_build = builder.build

        def counting_build(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            return real_build(*args, **kwargs)  # type: ignore[arg-type]

        builder.build = counting_build  # type: ignore[method-assign]
        adapter = PoolAdapter(
            wrapped=registry.get_adapter("person_email"),
            builder=builder,
            cache=PoolCache(max_bytes=10_000_000),
        )
        spec = ProviderSpec(
            locale="en_US",
            deterministic=True,
            namespace="customer_identity",
            seed=b"\x00\x00\x00\x00\x00\x00\x00\x2a",
        )
        for _ in range(10):
            adapter.generate("person_email", spec=spec, source_value=b"alice@example.com")
        assert calls["n"] == 1

    def test_identity_for_matches_built_pool_identity(self) -> None:
        """The cheap identity_for path must produce the same tuple as the
        built pool's identity, or the cache lookup would always miss."""
        builder = PoolBuilder(get_default_registry())
        seed = b"\x00\x00\x00\x00\x00\x00\x00\x2a"
        identity = builder.identity_for(
            "person_email", size=64, job_seed=seed, locale="en_US", namespace="ns"
        )
        pool = builder.build("person_email", size=64, job_seed=seed, locale="en_US", namespace="ns")
        assert identity == pool.identity

    def test_pool_size_from_extra_reaches_build(self) -> None:
        """NF4: pool_size carried in ProviderSpec.extra drives the built pool
        size (the prior code hardcoded size=10_000, so the knob was dead) and
        does not leak into the Faker call as a stray kwarg."""
        registry = get_default_registry()
        adapter = PoolAdapter(
            wrapped=registry.get_adapter("person_email"),
            builder=PoolBuilder(registry),
            cache=PoolCache(max_bytes=50_000_000),
        )
        spec = ProviderSpec(
            locale="en_US",
            deterministic=True,
            namespace="customer_identity",
            seed=b"\x00\x00\x00\x00\x00\x00\x00\x2a",
            extra={"pool_size": 128},
        )
        pool = adapter._build_or_get_pool("person_email", spec)
        assert pool.size == 128


class TestNonDeterministicDelegation:
    def test_non_deterministic_generate_delegates_to_wrapped(self) -> None:
        """Non-deterministic generate bypasses the pool, calls wrapped."""
        adapter = _adapter()
        spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None)
        out = adapter.generate("person_email", spec=spec, source_value=None)
        assert isinstance(out, str)
        assert "@" in out

    def test_generate_batch_always_delegates(self) -> None:
        """Per S4 spec §2: generate_batch has no per-row source value;
        always delegates to wrapped regardless of deterministic flag."""
        adapter = _adapter()
        spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=None)
        out = adapter.generate_batch("person_email", spec=spec, count=5)
        assert len(out) == 5


class TestProtocolConformance:
    """PoolAdapter must satisfy the BackendAdapter Protocol."""

    def test_has_protocol_attributes(self) -> None:
        adapter = _adapter()
        assert hasattr(adapter, "backend_type")
        assert hasattr(adapter, "backend_version")
        assert adapter.backend_type == "pool"
        assert adapter.backend_version.startswith("pool(")

    def test_has_protocol_methods(self) -> None:
        adapter = _adapter()
        assert callable(adapter.generate)
        assert callable(adapter.generate_batch)
        assert callable(adapter.capability_matrix)
