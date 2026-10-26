"""B3 acceptance: run-scoped out-of-core faker pool lifecycle.

Plan: docs/plans/2026-10-26-substrate-route-coverage.md (B3). This slice adds a
run-scoped context that owns and reuses a bounded provider pool across batches.
It does NOT admit faker on the out-of-core route; that is B4. These tests drive
the new `OocFakerPoolContext` directly.

Each new assertion fails on the base commit (d613c804) because
`decoy_engine.execution.out_of_core._faker_pool` does not exist there.

The contract under test:
- one pool build per identity, reused across many simulated batches;
- identical identities share a pool; a changed locale/provider/namespace/config
  does not;
- a second context has independent state (no cross-run cache leakage);
- an oversized pool plan is refused with a caller-visible code BEFORE the
  provider is allocated (the builder's `build` is never called);
- the pool the context builds is byte-identical to one PoolBuilder builds
  directly for the same identity (the context does not alter construction);
- pool memory is bounded by the run allowance, never by a batch/source row
  count.
"""

from __future__ import annotations

import numpy as np
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._faker_pool import OocFakerPoolContext
from decoy_engine.generation.pool._builder import PoolBuilder
from decoy_engine.providers_v2 import get_default_registry

_JOB_SEED = b"\x00\x01\x02\x03\x04\x05\x06\x07"
_OTHER_SEED = b"\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
_PROVIDER = "person_first_name"
_PROVIDER_2 = "person_last_name"
# Small pools keep the build cheap; the lifecycle, not the pool size, is tested.
_CFG = {"pool_size": 16}


class _CountingPoolBuilder(PoolBuilder):
    """Real PoolBuilder that counts `build` calls so a test can prove a pool is
    built exactly once (and never built at all when a plan is refused)."""

    def __init__(self, registry) -> None:  # type: ignore[no-untyped-def]
        super().__init__(registry)
        self.build_calls = 0

    def build(self, provider, **kwargs):  # type: ignore[no-untyped-def]
        self.build_calls += 1
        return super().build(provider, **kwargs)


@pytest.fixture
def registry():
    reg = get_default_registry()
    if not reg.get_capabilities(_PROVIDER).poolable:
        pytest.skip(f"{_PROVIDER} is not poolable in this environment")
    return reg


def _context(registry, *, max_pool_bytes=64 * 1024 * 1024, builder=None):
    return OocFakerPoolContext(
        registry=registry,
        job_seed=_JOB_SEED,
        max_pool_bytes=max_pool_bytes,
        builder=builder,
    )


def test_one_build_across_many_batches(registry):
    builder = _CountingPoolBuilder(registry)
    ctx = _context(registry, builder=builder)
    prepared = ctx.prepare(provider=_PROVIDER, namespace="ns", cfg=_CFG)

    pools = [ctx.get_pool(prepared) for _ in range(5)]  # five simulated batches

    assert builder.build_calls == 1
    assert ctx.builds == 1
    first = pools[0]
    for pool in pools[1:]:
        assert pool is first  # same cached object, not a rebuild
        np.testing.assert_array_equal(pool.values, first.values)


def test_identical_identity_shares_one_pool(registry):
    builder = _CountingPoolBuilder(registry)
    ctx = _context(registry, builder=builder)
    p1 = ctx.prepare(provider=_PROVIDER, namespace="ns", cfg=dict(_CFG))
    p2 = ctx.prepare(provider=_PROVIDER, namespace="ns", cfg=dict(_CFG))

    assert p1.identity == p2.identity
    pool1 = ctx.get_pool(p1)
    pool2 = ctx.get_pool(p2)
    assert pool1 is pool2
    assert builder.build_calls == 1


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param({"provider": _PROVIDER_2}, id="provider"),
        pytest.param({"namespace": "other_ns"}, id="namespace"),
        pytest.param({"cfg": {"pool_size": 16, "locale": "en_US"}}, id="locale"),
        pytest.param({"cfg": {"pool_size": 32}}, id="config_pool_size"),
    ],
)
def test_distinct_identity_does_not_share(registry, mutate):
    builder = _CountingPoolBuilder(registry)
    ctx = _context(registry, builder=builder)
    base_kwargs = {"provider": _PROVIDER, "namespace": "ns", "cfg": dict(_CFG)}
    changed_kwargs = {**base_kwargs, **mutate}
    if "poolable" in dir(registry):
        # person_last_name must be poolable for the provider-change case.
        if changed_kwargs["provider"] != _PROVIDER and not registry.get_capabilities(
            changed_kwargs["provider"]
        ).poolable:
            pytest.skip(f"{changed_kwargs['provider']} is not poolable here")

    base = ctx.prepare(**base_kwargs)
    changed = ctx.prepare(**changed_kwargs)

    assert base.identity != changed.identity
    ctx.get_pool(base)
    ctx.get_pool(changed)
    assert builder.build_calls == 2
    assert ctx.pool_count == 2


def test_second_context_has_independent_state(registry):
    builder1 = _CountingPoolBuilder(registry)
    ctx1 = _context(registry, builder=builder1)
    prepared1 = ctx1.prepare(provider=_PROVIDER, namespace="ns", cfg=dict(_CFG))
    ctx1.get_pool(prepared1)
    assert builder1.build_calls == 1

    builder2 = _CountingPoolBuilder(registry)
    ctx2 = _context(registry, builder=builder2)
    prepared2 = ctx2.prepare(provider=_PROVIDER, namespace="ns", cfg=dict(_CFG))
    ctx2.get_pool(prepared2)
    # A fresh context rebuilds; it does not read ctx1's pools.
    assert builder2.build_calls == 1
    assert ctx2.builds == 1

    ctx1.close()
    assert ctx1.pool_count == 0


def test_reservation_refused_before_provider_allocation(registry):
    builder = _CountingPoolBuilder(registry)
    # A 1-byte allowance cannot hold any pool: the reservation must refuse
    # before the provider is ever allocated.
    ctx = _context(registry, max_pool_bytes=1, builder=builder)
    prepared = ctx.prepare(provider=_PROVIDER, namespace="ns", cfg=dict(_CFG))

    with pytest.raises(ExecutionError) as exc:
        ctx.get_pool(prepared)
    assert exc.value.code == "out_of_core_faker_pool_budget_exceeded"
    assert builder.build_calls == 0  # provider never allocated
    assert ctx.builds == 0


def test_pool_bytes_bounded_by_allowance_not_row_count(registry):
    # Allowance sized for exactly one default-ish pool; a larger second distinct
    # pool is refused. The bound is the allowance, independent of any batch size.
    builder = _CountingPoolBuilder(registry)
    reserve_per_value = 4096
    ctx = OocFakerPoolContext(
        registry=registry,
        job_seed=_JOB_SEED,
        max_pool_bytes=16 * reserve_per_value,  # fits pool_size<=16 only
        builder=builder,
    )
    small = ctx.prepare(provider=_PROVIDER, namespace="ns", cfg={"pool_size": 16})
    ctx.get_pool(small)  # fits exactly
    big = ctx.prepare(provider=_PROVIDER, namespace="ns2", cfg={"pool_size": 17})
    with pytest.raises(ExecutionError) as exc:
        ctx.get_pool(big)
    assert exc.value.code == "out_of_core_faker_pool_budget_exceeded"


def test_invalid_pool_size_rejected(registry):
    ctx = _context(registry)
    with pytest.raises(ExecutionError) as exc:
        ctx.prepare(provider=_PROVIDER, namespace="ns", cfg={"pool_size": 0})
    assert exc.value.code == "out_of_core_faker_pool_size_invalid"


def test_context_pool_matches_direct_builder(registry):
    """The context must not alter pool construction: the pool it builds for an
    identity is byte-identical to one PoolBuilder builds directly."""
    ctx = _context(registry)
    prepared = ctx.prepare(provider=_PROVIDER, namespace="ns", cfg=dict(_CFG))
    ctx_pool = ctx.get_pool(prepared)

    direct = PoolBuilder(registry).build(
        _PROVIDER,
        size=prepared.pool_size,
        job_seed=_JOB_SEED,
        locale=prepared.locale,
        config=prepared.build_config,
        namespace=prepared.namespace,
    )
    assert ctx_pool.identity == direct.identity
    np.testing.assert_array_equal(ctx_pool.values, direct.values)


def test_distinct_job_seed_distinct_pool(registry):
    builder = _CountingPoolBuilder(registry)
    ctx1 = _context(registry, builder=builder)
    ctx2 = OocFakerPoolContext(
        registry=registry, job_seed=_OTHER_SEED, max_pool_bytes=64 * 1024 * 1024
    )
    p1 = ctx1.prepare(provider=_PROVIDER, namespace="ns", cfg=dict(_CFG))
    p2 = ctx2.prepare(provider=_PROVIDER, namespace="ns", cfg=dict(_CFG))
    # job_seed enters the pool seed, so the identity differs across runs.
    assert p1.identity != p2.identity
