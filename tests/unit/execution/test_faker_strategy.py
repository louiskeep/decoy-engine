"""engine-v2 S9 slice 2b: faker strategy (pool-backed) end-to-end.

Faker routes through PoolBuilder + the vectorized PoolSampler.sample (S9 path
#2). Tests the determinism contract (same job seed -> byte-identical; same
source -> same masked value), null preservation, and that output comes from the
provider's pool.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution import ExecutionResult, PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0x0123456789).to_bytes(8, "big")


def _faker_plan(*, deterministic: bool, pool_size: int = 256) -> Any:
    cs = ColumnSeed(
        namespace="people_ns" if deterministic else None,
        strategy="faker",
        provider="person_email",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=(("pool_size", pool_size),),
        coherent_with=(),
    )
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("people", TableSeed(per_column=(("email", cs),), per_group=())),),
        )
    )


def _plan(
    *,
    deterministic: bool = True,
    namespace: str | None = "people_ns",
    cardinality_mode: str = "reuse",
    provider_config: tuple[tuple[str, Any], ...] = (("pool_size", 256),),
    pool_size: int | None = None,
    scale: float | None = None,
) -> Any:
    """Build a single-column faker plan with the knobs each test needs to vary.

    Wider than `_faker_plan` so a test can drive one specific build/sample
    argument (locale, domain, namespace, the typed `pool_size`/`scale` fields,
    cardinality mode) while holding everything else fixed.
    """
    cs = ColumnSeed(
        namespace=namespace,
        strategy="faker",
        provider="person_email",
        backend_type="faker",
        backend_version="v",
        cardinality_mode=cardinality_mode,
        deterministic=deterministic,
        provider_config=provider_config,
        coherent_with=(),
        pool_size=pool_size,
        scale=scale,
    )
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("people", TableSeed(per_column=(("email", cs),), per_group=())),),
        )
    )


def _emails(plan: Any, table: pa.Table) -> list[Any]:
    return _run(plan, table).output.column("email").to_pylist()


def _run(plan: Any, table: pa.Table) -> ExecutionResult:
    return PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )


class TestFakerStrategy:
    def test_deterministic_reproducible_across_runs(self) -> None:
        src = pa.table({"email": ["a", "b", "a", None]})
        out1 = _run(_faker_plan(deterministic=True), src).output.column("email").to_pylist()
        out2 = _run(_faker_plan(deterministic=True), src).output.column("email").to_pylist()
        assert out1 == out2

    def test_deterministic_same_source_same_masked_value(self) -> None:
        src = pa.table({"email": ["a", "b", "a"]})
        out = _run(_faker_plan(deterministic=True), src).output.column("email").to_pylist()
        assert out[0] == out[2]  # repeated source value -> same masked value

    def test_null_preserved_deterministic(self) -> None:
        src = pa.table({"email": ["a", None, "c"]})
        out = _run(_faker_plan(deterministic=True), src).output.column("email").to_pylist()
        assert out[1] is None
        assert out[0] is not None and out[2] is not None

    def test_masked_values_come_from_email_pool(self) -> None:
        src = pa.table({"email": ["a", "b", "c"]})
        out = _run(_faker_plan(deterministic=True), src).output.column("email").to_pylist()
        assert all(isinstance(v, str) and "@" in v for v in out)

    def test_non_deterministic_masks_nonnull_preserves_null(self) -> None:
        src = pa.table({"email": ["a", None, "c"]})
        out = _run(_faker_plan(deterministic=False), src).output.column("email").to_pylist()
        assert out[1] is None
        assert isinstance(out[0], str) and "@" in out[0]
        assert isinstance(out[2], str) and "@" in out[2]

    def test_locale_selects_the_built_pool(self) -> None:
        # locale is a build knob (it feeds the pool seed), so two locales must
        # yield different masked values. Pins that the resolved locale actually
        # reaches PoolBuilder.build rather than being dropped or nulled.
        src = pa.table({"email": ["a", "b", "c", "d"]})
        en = _emails(_plan(provider_config=(("pool_size", 256), ("locale", "en_US"))), src)
        fr = _emails(_plan(provider_config=(("pool_size", 256), ("locale", "fr_FR"))), src)
        assert en != fr

    def test_locale_is_not_forwarded_as_a_provider_method_kwarg(self) -> None:
        # locale must be stripped from the config passed to the provider method;
        # the underlying Faker `email()` rejects a `locale=` kwarg, so leaking it
        # into build_config would raise rather than mask.
        src = pa.table({"email": ["a", "b", "c"]})
        out = _emails(_plan(provider_config=(("pool_size", 256), ("locale", "en_US"))), src)
        assert all(isinstance(v, str) and "@" in v for v in out)

    def test_provider_config_reaches_the_pool_build(self) -> None:
        # A non-locale/pool_size config key (here Faker's `domain`) must flow
        # through build_config into the generated values. Every masked value
        # carries the configured domain when the config is honored.
        src = pa.table({"email": ["a", "b", "c", "d"]})
        cfg = (("pool_size", 256), ("domain", "decoy-fixture.example"))
        out = _emails(_plan(provider_config=cfg), src)
        assert all(isinstance(v, str) and v.endswith("@decoy-fixture.example") for v in out)

    def test_namespace_selects_the_built_pool(self) -> None:
        # namespace feeds the pool seed at build time. In non-deterministic mode
        # the sampler ignores namespace, so any output difference isolates the
        # build-side namespace argument (the draw indices are the same seed).
        src = pa.table({"email": ["a", "b", "c", "d"]})
        a = _emails(_plan(deterministic=False, namespace="ns_a"), src)
        b = _emails(_plan(deterministic=False, namespace="ns_b"), src)
        assert a != b

    def test_scale_controls_target_cardinality(self) -> None:
        # SCALE_SOURCE_CARDINALITY with scale=0.5 collapses 4 distinct source
        # values onto round(4 * 0.5) = 2 distinct outputs. The default scale
        # (2.0) or a dropped scale would leave 4 distinct, so the count pins the
        # resolved scale flowing into the sampler.
        src = pa.table({"email": ["a", "b", "c", "d"]})
        out = _emails(
            _plan(
                deterministic=False,
                namespace=None,
                cardinality_mode="scale_source_cardinality",
                scale=0.5,
            ),
            src,
        )
        assert len({v for v in out if v is not None}) == 2

    def test_typed_pool_size_field_is_used(self) -> None:
        # A ColumnSeed carrying the typed `pool_size` field (no pool_size in
        # provider_config) must build a pool of that size. Nulling the resolved
        # value would pass size=None to build and fail.
        src = pa.table({"email": ["a", "b", "c", "d"]})
        out = _emails(_plan(pool_size=300, provider_config=()), src)
        assert len(out) == 4
        assert all(isinstance(v, str) and "@" in v for v in out)
