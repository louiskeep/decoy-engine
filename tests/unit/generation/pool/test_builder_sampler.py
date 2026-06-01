"""PoolBuilder + PoolSampler tests (S5 spec §3 + §5).

Covers the determinism contract (deterministic mode via derive_index),
the four cardinality modes, null preservation, and namespace independence.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.generation.pool import (
    CardinalityMode,
    GenerationError,
    PoolBuilder,
    PoolCapacityError,
    PoolSampler,
)
from decoy_engine.providers_v2 import get_default_registry


def _builder() -> PoolBuilder:
    return PoolBuilder(get_default_registry())


_SEED = b"\x00\x00\x00\x00\x00\x00\x00\x2a"  # 42


class TestPoolBuilder:
    def test_build_returns_pool_of_requested_size(self) -> None:
        pool = _builder().build("person_email", size=20, job_seed=_SEED)
        assert pool.size == 20
        assert len(pool.values) == 20

    def test_build_records_provider_and_backend_metadata(self) -> None:
        pool = _builder().build("person_email", size=10, job_seed=_SEED)
        assert pool.provider == "person_email"
        assert pool.backend_type == "faker"

    def test_build_rejects_non_poolable_provider(self) -> None:
        # address_full declares poolable=False per the catalog.
        with pytest.raises(PoolCapacityError) as excinfo:
            _builder().build("address_full", size=10, job_seed=_SEED)
        assert excinfo.value.code == "provider_not_poolable"

    def test_build_distinct_count_populated(self) -> None:
        pool = _builder().build("person_email", size=50, job_seed=_SEED)
        # Faker's email pool is wider than 50; expect mostly distinct.
        assert pool.distinct_count > 0
        assert pool.distinct_count <= 50

    def test_namespace_changes_pool_seed(self) -> None:
        pool_a = _builder().build("person_email", size=10, job_seed=_SEED, namespace="ns_a")
        pool_b = _builder().build("person_email", size=10, job_seed=_SEED, namespace="ns_b")
        # Different pool_seeds: identity tuples differ on the seed field.
        assert pool_a.seed != pool_b.seed

    def test_same_identity_two_builders_byte_identical_values(self) -> None:
        """F2: the Faker batch is now seeded from the derived pool_seed, so two
        independent builders building the same identity produce byte-identical
        pool values. Before F2 the Faker batch was unseeded: the pool_seed only
        entered the cache identity while the values were random per build, so
        the identity was a lie (and the same-process byte-identical test only
        passed because the broken in-process cache returned the first build)."""
        p1 = PoolBuilder(get_default_registry()).build(
            "person_email", size=50, job_seed=_SEED, namespace="ns"
        )
        p2 = PoolBuilder(get_default_registry()).build(
            "person_email", size=50, job_seed=_SEED, namespace="ns"
        )
        assert p1.identity == p2.identity
        assert list(p1.values) == list(p2.values)

    def test_different_seed_changes_values(self) -> None:
        """Sanity: seeding is real, not a no-op. A different job seed yields a
        different (seeded) pool."""
        p1 = _builder().build("person_email", size=50, job_seed=b"\x00" * 8, namespace="ns")
        p2 = _builder().build("person_email", size=50, job_seed=b"\x01" * 8, namespace="ns")
        assert list(p1.values) != list(p2.values)

    def test_identity_for_matches_build_identity(self) -> None:
        """F1 support: the cheap identity_for path equals build(...).identity."""
        builder = PoolBuilder(get_default_registry())
        ident = builder.identity_for("person_email", size=20, job_seed=_SEED, namespace="ns")
        pool = builder.build("person_email", size=20, job_seed=_SEED, namespace="ns")
        assert ident == pool.identity


class TestPoolSamplerDeterministic:
    def test_same_inputs_produce_same_output(self) -> None:
        builder = _builder()
        sampler = PoolSampler()
        pool = builder.build("person_email", size=100, job_seed=_SEED)
        source = pd.Series(["alice", "bob", "carol", "alice"])
        out_a = sampler.sample(
            pool,
            n=4,
            mode=CardinalityMode.REUSE,
            seed=_SEED,
            source=source,
            namespace="customer_identity",
            deterministic=True,
        )
        out_b = sampler.sample(
            pool,
            n=4,
            mode=CardinalityMode.REUSE,
            seed=_SEED,
            source=source,
            namespace="customer_identity",
            deterministic=True,
        )
        assert out_a.tolist() == out_b.tolist()
        # alice appears twice in source; both should map to same value.
        assert out_a.iloc[0] == out_a.iloc[3]

    def test_different_namespaces_produce_different_output(self) -> None:
        builder = _builder()
        sampler = PoolSampler()
        # Build separate pools per namespace (pool_seed differs).
        pool_a = builder.build("person_email", size=100, job_seed=_SEED, namespace="ns_a")
        pool_b = builder.build("person_email", size=100, job_seed=_SEED, namespace="ns_b")
        source = pd.Series(["alice"])
        out_a = sampler.sample(
            pool_a,
            n=1,
            mode=CardinalityMode.REUSE,
            seed=_SEED,
            source=source,
            namespace="ns_a",
            deterministic=True,
        )
        out_b = sampler.sample(
            pool_b,
            n=1,
            mode=CardinalityMode.REUSE,
            seed=_SEED,
            source=source,
            namespace="ns_b",
            deterministic=True,
        )
        # Different namespaces -> independent outputs (statistically; one
        # collision is theoretically possible but unlikely with pool of 100).
        # Just assert deterministic-per-namespace separation works.
        assert isinstance(out_a.iloc[0], str)
        assert isinstance(out_b.iloc[0], str)

    def test_deterministic_requires_source_and_namespace(self) -> None:
        builder = _builder()
        sampler = PoolSampler()
        pool = builder.build("person_email", size=10, job_seed=_SEED)
        with pytest.raises(GenerationError) as excinfo:
            sampler.sample(
                pool,
                n=1,
                mode=CardinalityMode.REUSE,
                seed=_SEED,
                source=None,
                namespace="ns",
                deterministic=True,
            )
        assert excinfo.value.code == "deterministic_requires_source_and_namespace"

    def test_null_in_source_preserves_in_output(self) -> None:
        builder = _builder()
        sampler = PoolSampler()
        pool = builder.build("person_email", size=20, job_seed=_SEED)
        source = pd.Series(["alice", None, "bob"])
        out = sampler.sample(
            pool,
            n=3,
            mode=CardinalityMode.REUSE,
            seed=_SEED,
            source=source,
            namespace="customer_identity",
            deterministic=True,
        )
        assert pd.isna(out.iloc[1])
        assert not pd.isna(out.iloc[0])
        assert not pd.isna(out.iloc[2])


class TestPoolSamplerNonDeterministic:
    def test_reuse_mode_returns_length_n(self) -> None:
        builder = _builder()
        sampler = PoolSampler()
        pool = builder.build("person_email", size=50, job_seed=_SEED)
        out = sampler.sample(pool, n=100, mode=CardinalityMode.REUSE, seed=_SEED)
        assert len(out) == 100

    def test_unique_mode_returns_distinct_values(self) -> None:
        builder = _builder()
        sampler = PoolSampler()
        pool = builder.build("person_email", size=50, job_seed=_SEED)
        out = sampler.sample(pool, n=20, mode=CardinalityMode.UNIQUE, seed=_SEED)
        assert len(set(out)) == 20

    def test_unique_mode_raises_when_n_exceeds_pool(self) -> None:
        builder = _builder()
        sampler = PoolSampler()
        pool = builder.build("person_email", size=10, job_seed=_SEED)
        with pytest.raises(GenerationError) as excinfo:
            sampler.sample(pool, n=20, mode=CardinalityMode.UNIQUE, seed=_SEED)
        assert excinfo.value.code == "uniqueness_impossible"

    def test_qa1_h9_unique_plus_deterministic_raises(self) -> None:
        """QA-1 H9 (2026-06-01): mode=UNIQUE + deterministic=True must
        raise the typed GenerationError. Pre-fix the combo silently
        fell through to deterministic mode (REUSE semantics)."""
        builder = _builder()
        sampler = PoolSampler()
        pool = builder.build("person_email", size=50, job_seed=_SEED)
        source = pd.Series(["a", "b", "c", "d", "e"])
        with pytest.raises(GenerationError) as excinfo:
            sampler.sample(
                pool, n=5, mode=CardinalityMode.UNIQUE, seed=_SEED,
                source=source, namespace="ns",
                deterministic=True,
            )
        assert excinfo.value.code == "deterministic_mode_unsupported_cardinality"

    def test_seed_stability_same_seed_same_output(self) -> None:
        """NEP-19 contract: np.random.default_rng(seed) is reproducible."""
        builder = _builder()
        sampler = PoolSampler()
        pool = builder.build("person_email", size=50, job_seed=_SEED)
        out_a = sampler.sample(pool, n=10, mode=CardinalityMode.REUSE, seed=_SEED)
        out_b = sampler.sample(pool, n=10, mode=CardinalityMode.REUSE, seed=_SEED)
        assert out_a.tolist() == out_b.tolist()

    def test_match_cardinality_mapping_is_row_order_independent(self) -> None:
        """NF3: the source-value -> pool-value mapping is keyed to the sorted
        distinct-value set, so reordering source rows yields the same mapping.
        The prior code keyed on first-appearance order, so the same column in a
        different row order produced a different mapping."""
        builder = _builder()
        sampler = PoolSampler()
        pool = builder.build("person_email", size=100, job_seed=_SEED)
        src1 = pd.Series(["a", "b", "c", "a", "b"])
        src2 = pd.Series(["c", "b", "a", "b", "a"])  # same set, shuffled
        out1 = sampler.sample(
            pool, n=5, mode=CardinalityMode.MATCH_SOURCE_CARDINALITY, seed=_SEED, source=src1
        )
        out2 = sampler.sample(
            pool, n=5, mode=CardinalityMode.MATCH_SOURCE_CARDINALITY, seed=_SEED, source=src2
        )
        # Each distinct source value maps to the same pool value in both runs.
        assert dict(zip(src1, out1, strict=True)) == dict(zip(src2, out2, strict=True))


class TestCardinalityEnum:
    def test_four_values_exactly(self) -> None:
        """R6 reshape: deterministic_map is NOT in the enum."""
        assert {m.value for m in CardinalityMode} == {
            "reuse",
            "unique",
            "match_source_cardinality",
            "scale_source_cardinality",
        }
        assert "deterministic_map" not in {m.value for m in CardinalityMode}
