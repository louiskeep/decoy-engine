"""Provider execution classification tests (native program, Task 0.5).

`classify_provider` is the axis eligibility + fallback decisions read to know
HOW a provider can execute: `pool_native` (a bounded value pool built once and
selected natively per row), `python_only` (bounded/reproducible but currently
Python-executed), or `reject_large` (an arbitrary callable that cannot be
honestly made native and is rejected on large jobs). Unknown/custom providers
must fail closed to `reject_large`, never `pool_native`.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.execution.native._provider_class import (
    ProviderClass,
    classify_provider,
)
from decoy_engine.providers_v2 import (
    CapabilityMatrix,
    ProviderRegistry,
    ProviderSpec,
    get_default_registry,
)

_REGISTRY = get_default_registry()
_ALL_CLASSES: frozenset[ProviderClass] = frozenset({"pool_native", "python_only", "reject_large"})


class _StubAdapter:
    """Minimal BackendAdapter stand-in for registry-shape tests.

    classify_provider only reads a registered provider's CapabilityMatrix,
    never its adapter, so this stub exists purely to satisfy
    ProviderRegistry's (adapter, capabilities) binding shape.
    """

    backend_type: str = "stub"
    backend_version: str = "test/v1"

    def generate(
        self, provider: str, *, spec: ProviderSpec, source_value: bytes | None = None
    ) -> Any:
        raise NotImplementedError

    def generate_batch(self, provider: str, *, spec: ProviderSpec, count: int) -> list[Any]:
        raise NotImplementedError

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Explicit examples named by the plan (Task 0.5 Step 1).
# ---------------------------------------------------------------------------
def test_faker_poolable_provider_is_pool_native() -> None:
    assert classify_provider("person_email", None) == "pool_native"


def test_faker_non_poolable_provider_is_python_only() -> None:
    assert classify_provider("uuid", None) == "python_only"


def test_decoy_native_identifier_provider_is_pool_native() -> None:
    assert classify_provider("synthetic_ssn", None) == "pool_native"


def test_composite_provider_is_python_only() -> None:
    assert classify_provider("composite_name_email", None) == "python_only"


def test_unknown_custom_provider_is_reject_large_by_default() -> None:
    for provider_id in ("custom.my_callable", "totally_made_up_provider", ""):
        assert classify_provider(provider_id, None) == "reject_large", provider_id


def test_unknown_provider_config_hint_never_upgrades_to_pool_native() -> None:
    # A caller-supplied provider_config must not flip the fail-closed default
    # into pool_native; Phase 0 ships no small-job override, so any extra
    # config on an unknown provider still rejects.
    assert classify_provider("nonexistent_provider", {"small_job": True}) == "reject_large"


def test_none_provider_id_does_not_crash_and_is_not_pool_native() -> None:
    # Real callers sometimes carry a None provider (scalar transforms have no
    # provider at all, per ColumnSeed). classify_provider must stay total and
    # fail closed rather than raise.
    assert classify_provider(None, None) != "pool_native"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Registry-metadata-derived correctness. Each assertion is a claim about what
# a CapabilityMatrix field means (business invariant), not a re-statement of
# the implementation's branch structure.
# ---------------------------------------------------------------------------
def test_every_decoy_native_identifier_provider_is_pool_native() -> None:
    decoy_native = [
        name
        for name in _REGISTRY.known_providers()
        if _REGISTRY.get_capabilities(name).backend_type == "decoy_native"
    ]
    assert decoy_native, "expected decoy_native providers in the default registry"
    for name in decoy_native:
        assert classify_provider(name, None) == "pool_native", name


def test_every_composite_provider_is_python_only() -> None:
    # Task 0.2's `<composite>` StrategyCapabilities entry (_capabilities.py)
    # declares output_type_is_static=False, the same signal _fallback_policy
    # reads to keep a node off the native route; 0.5 must not contradict that
    # boundary by calling a composite provider pool_native.
    composites = [
        name
        for name in _REGISTRY.known_providers()
        if _REGISTRY.get_capabilities(name).backend_type == "composite"
    ]
    assert composites, "expected composite providers in the default registry"
    for name in composites:
        assert classify_provider(name, None) == "python_only", name


def test_faker_provider_class_follows_the_registry_poolable_flag() -> None:
    faker_providers = [
        name
        for name in _REGISTRY.known_providers()
        if _REGISTRY.get_capabilities(name).backend_type == "faker"
    ]
    assert faker_providers, "expected faker providers in the default registry"
    poolable = {n for n in faker_providers if _REGISTRY.get_capabilities(n).poolable}
    non_poolable = set(faker_providers) - poolable
    assert poolable and non_poolable, "expected both poolable and non-poolable faker providers"
    for name in poolable:
        assert classify_provider(name, None) == "pool_native", name
    for name in non_poolable:
        assert classify_provider(name, None) == "python_only", name


def test_mimesis_provider_class_follows_the_poolable_flag() -> None:
    # Mimesis is an optional dependency and the default adoption matrix is
    # empty (ADOPTED_MIMESIS_PROVIDERS == frozenset()), so the live registry
    # may carry zero mimesis-bound providers today. Exercise the branch
    # directly against a standalone registry so it is covered either way.
    poolable_cap = _fake_capability("mimesis_probe_poolable", backend_type="mimesis", poolable=True)
    non_poolable_cap = _fake_capability(
        "mimesis_probe_fixed", backend_type="mimesis", poolable=False
    )
    registry = ProviderRegistry(
        {
            poolable_cap.provider: (_StubAdapter(), poolable_cap),
            non_poolable_cap.provider: (_StubAdapter(), non_poolable_cap),
        }
    )
    assert classify_provider("mimesis_probe_poolable", None, registry=registry) == "pool_native"
    assert classify_provider("mimesis_probe_fixed", None, registry=registry) == "python_only"


def test_unrecognized_backend_type_is_reject_large() -> None:
    # Defense in depth: a registered provider whose backend_type this
    # classifier does not recognize (a future backend the registry ships
    # before 0.5 is updated) must fail closed, not fall through to
    # pool_native.
    cap = _fake_capability("future_provider", backend_type="quantum_oracle", poolable=True)
    registry = ProviderRegistry({cap.provider: (_StubAdapter(), cap)})
    assert classify_provider("future_provider", None, registry=registry) == "reject_large"


# ---------------------------------------------------------------------------
# Totality (Task 0.5 Step 4): walk the ACTUAL live registry so a newly added,
# unhandled provider fails this test rather than silently misclassifying.
# ---------------------------------------------------------------------------
def test_classify_provider_is_total_over_the_live_registry() -> None:
    registry = get_default_registry()
    providers = sorted(registry.known_providers())
    assert providers, "expected the live default registry to be non-empty"
    seen: dict[str, str] = {}
    for name in providers:
        result = classify_provider(name, None)
        assert result is not None, name
        assert result in _ALL_CLASSES, f"{name}: unexpected class {result!r}"
        seen[name] = result
    assert len(seen) == len(providers)


def _fake_capability(provider: str, *, backend_type: str, poolable: bool) -> CapabilityMatrix:
    return CapabilityMatrix(
        provider=provider,
        backend_type=backend_type,
        backend_version="test/v1",
        supports_deterministic=False,
        supports_uniqueness=True,
        supports_value_reuse=True,
        preserves_source_cardinality=False,
        participates_in_fk_pk=False,
        poolable=poolable,
        supported_locales=("en_US",),
        supports_coherent_link=False,
        format_regex=None,
        blocklist_validators=(),
        fallback_behavior="fail_plan_compile",
    )
