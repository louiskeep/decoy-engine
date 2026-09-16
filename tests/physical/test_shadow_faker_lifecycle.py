"""Task 4.6 slice 1: `ShadowCoordinator`'s deterministic-faker lifecycle,
proven at the coordinator boundary and ALWAYS RUNNING (no companion-present
skip): every test here monkeypatches `load_compiled_index_kernel` directly,
either to raise (the fail-closed cases) or to return a counting wrapper
around the pure-Python `reference_index_derivation()` (the lifecycle/
isolation cases), so none of them need the compiled companion installed.

Companion-guarded cell-for-cell parity against the live compiled kernel
lives in `tests/physical/test_shadow_corpus.py`; this file proves the
CONTRACT (load-once, resolve-once-per-node, build-once-per-identity,
fail-closed on a missing/incomplete companion, never the module-global
default pool cache) independent of whether that kernel is present here.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import reference_index_derivation
from decoy_engine.execution.physical import _shadow_coordinator
from decoy_engine.execution.physical._plan import (
    ExecutionBinding,
    KeyBinding,
    PhysicalNode,
    PhysicalPlan,
    PhysicalTable,
    PoolBinding,
)
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from decoy_engine.execution.physical._shadow_diff_codes import (
    NATIVE_COMPANION_UNAVAILABLE,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._types import DriverId
from decoy_engine.generation.pool import PoolBuilder
from decoy_engine.generation.pool._cache import (
    _reset_default_pool_cache_for_tests,
    get_default_pool_cache,
)
from decoy_engine.providers_v2 import get_default_registry


def _faker_binding(
    *,
    column: str = "c",
    provider: str = "person_first_name",
    plan_pool_size: int = 30,
    namespace: str = "ns_a",
) -> ExecutionBinding:
    return ExecutionBinding(
        operator_id="native_faker_select",
        operator_reason="slice_native_admitted:faker",
        resolved_config=(),
        input_schema=pa.schema([pa.field(column, pa.string())]),
        output_schema=pa.schema([pa.field(column, pa.string())]),
        determinism_family="source_keyed_hmac",
        determinism_version=2,
        key_binding=KeyBinding(key_source="mask_key", namespace=namespace),
        diagnostic_obligations=(),
        required_prepasses=(),
        batch_estimate=None,
        pool_binding=PoolBinding(provider=provider, plan_pool_size=plan_pool_size),
    )


def _faker_node(
    column: str,
    *,
    provider: str = "person_first_name",
    plan_pool_size: int = 30,
    namespace: str = "ns_a",
) -> PhysicalNode:
    return PhysicalNode(
        node_id=f"t:{column}:scalar:faker",
        table="t",
        columns=(column,),
        kind="scalar",
        strategy="faker",
        fallback_policy="native",
        provider_class="pool_native",
        execution=_faker_binding(
            column=column, provider=provider, plan_pool_size=plan_pool_size, namespace=namespace
        ),
    )


def _plan_with_nodes(*nodes: PhysicalNode) -> PhysicalPlan:
    table = PhysicalTable(
        table="t",
        driver=DriverId.FULL_FRAME,
        driver_reason="test",
        driver_reason_detail=None,
        rejected_alternatives=(),
        relationship_role="independent",
        substrate="pandas",
        nodes=nodes,
    )
    return PhysicalPlan(
        engine_version="test", plan_hash="deadbeef", synthesis=None, tables=(table,)
    )


class _CountingIndexKernel:
    """Wraps the pure-Python reference derivation so lifecycle tests run
    with no compiled companion at all, while still counting real
    `derive_index_batch` invocations."""

    def __init__(self) -> None:
        self.batch_calls = 0
        self._ref = reference_index_derivation()

    def derive_index_batch(self, values, *, mask_key, namespace, pool_size, native_threads=None):
        self.batch_calls += 1
        return self._ref.derive_index_batch(
            values,
            mask_key=mask_key,
            namespace=namespace,
            pool_size=pool_size,
            native_threads=native_threads,
        )


def _counting_loader(kernel):
    calls = {"n": 0}

    def _loader():
        calls["n"] += 1
        return kernel

    return _loader, calls


# ---------------------------------------------------------------------------
# Fail-closed: a missing or index-less companion never builds a pool, never
# produces output, and translates into the coded difference -- proven at the
# coordinator boundary, not `_index_ext` in isolation.
# ---------------------------------------------------------------------------


def test_load_compiled_index_kernel_raising_yields_coded_failure_and_never_builds_a_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise() -> None:
        raise CryptoExtensionUnavailableError("simulated: companion missing")

    monkeypatch.setattr(_shadow_coordinator, "load_compiled_index_kernel", _raise)

    def _guard_build(self, provider, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a pool must never be built when the index kernel failed to load")

    monkeypatch.setattr(PoolBuilder, "build", _guard_build)

    plan = _plan_with_nodes(_faker_node("c"))
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    snapshot = capture_shadow_snapshot({"t": source})
    ctx = ShadowContext(mask_key=b"\x01" * 32, job_seed=(1).to_bytes(8, "big"))
    coordinator = ShadowCoordinator(ctx=ctx, registry=get_default_registry())

    with pytest.raises(ShadowDifference) as excinfo:
        coordinator.run(plan, snapshot)
    assert excinfo.value.code == NATIVE_COMPANION_UNAVAILABLE


def test_abi2_companion_without_derive_index_batch_yields_the_same_coded_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real loader raises `CryptoExtensionUnavailableError` for BOTH a
    missing companion and an abi-2 companion that predates the index kernel
    (`_index_ext.py`'s own distinct messages); the coordinator must not
    special-case either message -- both translate to the identical coded
    `ShadowDifference`."""

    def _raise() -> None:
        raise CryptoExtensionUnavailableError(
            "the decoy-engine-native companion lacks derive_index_batch; it "
            "predates the index kernel"
        )

    monkeypatch.setattr(_shadow_coordinator, "load_compiled_index_kernel", _raise)

    plan = _plan_with_nodes(_faker_node("c"))
    source = pa.table({"c": pa.array(["a", "b"], type=pa.string())})
    snapshot = capture_shadow_snapshot({"t": source})
    ctx = ShadowContext(mask_key=b"\x02" * 32, job_seed=(2).to_bytes(8, "big"))

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=get_default_registry()).run(plan, snapshot)
    assert excinfo.value.code == NATIVE_COMPANION_UNAVAILABLE


def test_bound_faker_node_with_no_registry_raises_assertion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ShadowCoordinator.registry` defaults to `None` so the pre-existing
    unified-slice caller (`ShadowCoordinator(ctx=ctx)`, no faker admitted in
    this slice) is unaffected; a bound faker node must assert the registry
    is present rather than silently masking against the wrong one."""
    kernel = _CountingIndexKernel()
    loader, _ = _counting_loader(kernel)
    monkeypatch.setattr(_shadow_coordinator, "load_compiled_index_kernel", loader)

    plan = _plan_with_nodes(_faker_node("c"))
    source = pa.table({"c": pa.array(["a", "b"], type=pa.string())})
    snapshot = capture_shadow_snapshot({"t": source})
    ctx = ShadowContext(mask_key=b"\x03" * 32, job_seed=(3).to_bytes(8, "big"))

    with pytest.raises(AssertionError, match="registry"):
        ShadowCoordinator(ctx=ctx).run(plan, snapshot)


# ---------------------------------------------------------------------------
# Lifecycle counters across a ragged multi-batch table: loader once per run,
# pool resolution once per node, pool build once per unique identity
# (including under an A -> B -> A node order), and one derive_index_batch
# call per faker batch.
# ---------------------------------------------------------------------------


def test_lifecycle_counters_under_a_shared_and_distinct_identity_a_b_a_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = _CountingIndexKernel()
    loader, load_calls = _counting_loader(kernel)
    monkeypatch.setattr(_shadow_coordinator, "load_compiled_index_kernel", loader)

    real_resolve = _shadow_coordinator.resolve_faker_pool_identity
    resolve_calls: list[str] = []

    def _counting_resolve(**kwargs):
        resolve_calls.append(kwargs["namespace"])
        return real_resolve(**kwargs)

    monkeypatch.setattr(_shadow_coordinator, "resolve_faker_pool_identity", _counting_resolve)

    real_build = PoolBuilder.build
    build_calls: list[tuple[str, str | None]] = []

    def _counting_build(self, provider, **kwargs):
        build_calls.append((provider, kwargs.get("namespace")))
        return real_build(self, provider, **kwargs)

    monkeypatch.setattr(PoolBuilder, "build", _counting_build)

    # Node order A -> B -> A2: A and A2 share provider+namespace+pool_size
    # (the same PoolIdentity); B is a distinct identity. Proves build-once
    # holds under adversarial order, not merely "first node wins."
    node_a = _faker_node("colA", namespace="ns_shared")
    node_b = _faker_node("colB", namespace="ns_b")
    node_a2 = _faker_node("colA2", namespace="ns_shared")
    plan = _plan_with_nodes(node_a, node_b, node_a2)

    n_rows = 5
    values = [f"src_{i % 3}" for i in range(n_rows)]
    source = pa.table(
        {
            "colA": pa.array(values, type=pa.string()),
            "colB": pa.array(values, type=pa.string()),
            "colA2": pa.array(values, type=pa.string()),
        }
    )
    snapshot = capture_shadow_snapshot({"t": source})
    ctx = ShadowContext(mask_key=b"\x04" * 32, job_seed=(4).to_bytes(8, "big"), batch_size_rows=2)
    result = ShadowCoordinator(ctx=ctx, registry=get_default_registry()).run(plan, snapshot)

    assert load_calls["n"] == 1  # loaded once per RUN, not once per node
    assert resolve_calls == ["ns_shared", "ns_b", "ns_shared"]  # once per NODE
    assert len(build_calls) == 2  # once per unique IDENTITY -- A2 reuses A's build
    assert {ns for _, ns in build_calls} == {"ns_shared", "ns_b"}
    # batch_size=2 over 5 rows -> batches of [2, 2, 1] = 3 per node x 3 nodes.
    assert kernel.batch_calls == 9

    for node_id in ("t:colA:scalar:faker", "t:colB:scalar:faker", "t:colA2:scalar:faker"):
        evidence = result.route_evidence[node_id]
        assert evidence.executed is True
        assert evidence.compiled_kernel_executed is True

    out = result.outputs["t"]
    assert out.column("colA").to_pylist() == out.column("colA2").to_pylist()


# ---------------------------------------------------------------------------
# Global-cache isolation: the coordinator's own fresh, run-scoped PoolCache
# is used -- never the module-global default, which a caller-visible pool
# identity collision could otherwise poison across unrelated runs.
# ---------------------------------------------------------------------------


def test_run_never_consults_the_module_global_default_pool_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_default_pool_cache_for_tests()
    default_cache = get_default_pool_cache()

    def _poisoned_get(identity):  # pragma: no cover - must never run
        raise AssertionError(
            "ShadowCoordinator.run must never consult the module-global default PoolCache"
        )

    monkeypatch.setattr(default_cache, "get", _poisoned_get)

    kernel = _CountingIndexKernel()
    loader, _ = _counting_loader(kernel)
    monkeypatch.setattr(_shadow_coordinator, "load_compiled_index_kernel", loader)

    plan = _plan_with_nodes(_faker_node("c"))
    source = pa.table({"c": pa.array(["a", "b", "c", "a"], type=pa.string())})
    snapshot = capture_shadow_snapshot({"t": source})
    ctx = ShadowContext(mask_key=b"\x05" * 32, job_seed=(5).to_bytes(8, "big"))

    result = ShadowCoordinator(ctx=ctx, registry=get_default_registry()).run(plan, snapshot)
    assert result.outputs["t"].num_rows == 4
