"""C5b-i: non-deterministic REUSE Faker is position-keyed on `job_seed`.

Plan: docs/plans/2026-10-05-c5b-i-positional-nondet-faker.md (rev 2.1, section 5).

For the non-null row at ordinal ``g = ctx.row_offset + i`` the value is
``pool.values[derive_index(job_seed, selection_namespace, encode_int(g), pool.size)]``.
The expected values here come from the scalar ``derive_index`` and the independently built
pool, never from a snapshot of the handler's own output. The frozen index tables were
computed once from direct ``derive_index`` calls so a change to the key encoding, the
default namespace or the key order fails here instead of being recomputed by the code
under test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import kernel
from decoy_engine.determinism import derive_index
from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies import _faker_positional as fp
from decoy_engine.execution._strategies._faker import FakerStrategyHandler
from decoy_engine.execution._strategies._nested import NestedStrategyHandler
from decoy_engine.execution._strategies._orphan import make_remap_fn
from decoy_engine.execution._when_gate import run_with_when_gate
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.native._index_ext import (
    load_compiled_index_kernel,
    reference_index_derivation,
)
from decoy_engine.generation.pool import CardinalityMode, PoolBuilder, PoolSampler, ValuePool
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._identity import resolve_faker_pool_identity
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NSREG = NamespaceRegistry(bindings=())
JOB = (0x0123456789).to_bytes(8, "big")
MASK = (0x77).to_bytes(8, "big")
ZERO = bytes(8)
POOL = 64

# derive_index(JOB, "faker-nd/1:t/1:c", encode_int(g), pool_size=64) for g in 0..11
KAT_DEFAULT = [25, 4, 53, 24, 41, 33, 42, 28, 54, 47, 35, 19]
# derive_index(JOB, "people", encode_int(g), pool_size=64) for g in 0..11
KAT_PEOPLE = [55, 58, 10, 62, 35, 35, 56, 1, 32, 19, 35, 8]
# the same two namespaces for g in 1000..1007
KAT_DEFAULT_1000 = [13, 23, 49, 11, 6, 17, 32, 28]
KAT_PEOPLE_1000 = [7, 46, 9, 38, 23, 28, 32, 23]

NEEDS_COMPANION = pytest.mark.skipif(
    not native_companion_status().ok, reason="native companion not installed"
)


def _seed(
    *,
    namespace: str | None = None,
    deterministic: bool = False,
    mode: str = "reuse",
    provider: str = "person_first_name",
    when: str | None = None,
    strategy: str = "faker",
    config: tuple[tuple[str, Any], ...] | None = None,
    scale: float | None = None,
    plan_pool_size: int | None = None,
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=provider,
        backend_type="faker",
        backend_version="v",
        cardinality_mode=mode,  # type: ignore[arg-type]
        deterministic=deterministic,
        provider_config=config if config is not None else (("pool_size", POOL),),
        coherent_with=(),
        when=when,
        scale=scale,
        pool_size=plan_pool_size,
    )


def _ctx(
    *,
    table: str = "t",
    row_offset: int = 0,
    job_seed: bytes = JOB,
    mask_key: bytes | None = None,
) -> StrategyContext:
    return StrategyContext(
        registry=_REG,
        pool_cache=PoolCache(),
        relationship_graph=_GRAPH,
        namespace_registry=_NSREG,
        job_seed=job_seed,
        mask_key=job_seed if mask_key is None else mask_key,
        current_table=table,
        row_offset=row_offset,
    )


def _run(
    values: list[Any],
    seed: ColumnSeed | None = None,
    *,
    ctx: StrategyContext | None = None,
    column: str = "c",
) -> list[Any]:
    df = pd.DataFrame({column: values})
    out, _ = FakerStrategyHandler().run(df, column, seed or _seed(), ctx or _ctx())
    return out[column].tolist()


def _pool(seed: ColumnSeed, job_seed: bytes = JOB) -> ValuePool:
    """The pool the handler builds, rebuilt here with the ORIGINAL plan namespace."""
    cfg = provider_config_to_dict(seed.provider_config)
    builder = PoolBuilder(_REG)
    size, locale, build_config, _ = resolve_faker_pool_identity(
        builder=builder,
        provider=seed.provider or "",
        plan_pool_size=seed.pool_size,
        namespace=seed.namespace,
        job_seed=job_seed,
        cfg=cfg,
    )
    return builder.build(
        provider=seed.provider or "",
        size=size,
        job_seed=job_seed,
        locale=locale,
        config=build_config,
        namespace=seed.namespace,
    )


def _default_ns(table: str, column: str) -> str:
    """The documented default, written out literally (not via the helper under test)."""
    return f"faker-nd/{len(table)}:{table}/{len(column)}:{column}"


def _expected(
    pool: ValuePool,
    namespace: str,
    ordinals: Any,
    *,
    key: bytes = JOB,
) -> list[Any]:
    return [
        pool.values[derive_index(key, namespace, kernel.encode_int(g), pool_size=pool.size)]
        for g in ordinals
    ]


# ---- 1. formula KAT -----------------------------------------------------------------


class TestFormulaKat:
    def test_frozen_default_namespace_indices_equal_the_scalar_primitive(self) -> None:
        got = [
            derive_index(JOB, _default_ns("t", "c"), kernel.encode_int(g), pool_size=POOL)
            for g in range(12)
        ]
        assert got == KAT_DEFAULT

    def test_default_namespace_draw_equals_the_formula(self) -> None:
        pool = _pool(_seed())
        out = _run(["x"] * 12)
        assert out == [pool.values[i] for i in KAT_DEFAULT]
        assert out == _expected(pool, "faker-nd/1:t/1:c", range(12))

    def test_explicit_namespace_draw_equals_the_formula(self) -> None:
        seed = _seed(namespace="people")
        pool = _pool(seed)
        assert _run(["x"] * 12, seed) == [pool.values[i] for i in KAT_PEOPLE]

    def test_longer_column_equals_the_formula_row_for_row(self) -> None:
        pool = _pool(_seed())
        n = 500
        assert _run(["x"] * n) == _expected(pool, "faker-nd/1:t/1:c", range(n))

    def test_the_output_ignores_source_values(self) -> None:
        assert _run([f"a{i}" for i in range(12)]) == _run([f"zzz-{i * 7}" for i in range(12)])

    def test_reference_path_equals_the_formula(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fp, "_compiled_index_kernel", lambda: None)
        pool = _pool(_seed())
        assert _run(["x"] * 12) == [pool.values[i] for i in KAT_DEFAULT]

    @NEEDS_COMPANION
    def test_compiled_path_equals_the_formula_and_the_reference_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = _pool(_seed())
        compiled = _run(["x"] * 300)
        monkeypatch.setattr(fp, "_compiled_index_kernel", lambda: None)
        reference = _run(["x"] * 300)
        assert compiled == reference == _expected(pool, "faker-nd/1:t/1:c", range(300))


# ---- 2. reproducibility -------------------------------------------------------------


class TestReproducible:
    @pytest.mark.parametrize("job_seed", [JOB, ZERO], ids=["seed-set", "seed-unset-zeros"])
    def test_two_runs_give_identical_bytes(self, job_seed: bytes) -> None:
        a = _run(["x"] * 200, ctx=_ctx(job_seed=job_seed))
        b = _run(["x"] * 200, ctx=_ctx(job_seed=job_seed))
        assert a == b

    def test_end_to_end_adapter_runs_are_identical(self) -> None:
        cs = _seed()
        plan: Any = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=JOB,
                per_table=(("t", TableSeed(per_column=(("c", cs),), per_group=())),),
            )
        )
        src = pa.table({"c": [f"v{i}" for i in range(100)]})

        def go() -> list[Any]:
            res = PandasExecutionAdapter().run_single(
                plan,
                src,
                registry=_REG,
                relationship_graph=_GRAPH,
                namespace_registry=_NSREG,
            )
            return res.output.column("c").to_pylist()

        assert go() == go()

    def test_a_different_job_seed_changes_the_output(self) -> None:
        assert _run(["x"] * 50) != _run(["x"] * 50, ctx=_ctx(job_seed=ZERO))


# ---- 3. the identical-columns bug is fixed ----------------------------------------------


def _multi_plan(tables: dict[str, list[tuple[str, ColumnSeed]]]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=JOB,
            per_table=tuple(
                (name, TableSeed(per_column=tuple(cols), per_group=()))
                for name, cols in tables.items()
            ),
        )
    )


def _fk_plan(payload: ColumnSeed) -> tuple[Any, Any]:
    from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

    key = ColumnSeed(
        namespace="kns",
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )
    plan = _multi_plan(
        {
            "parent": [("pk", key), ("pay", payload)],
            "child": [("fk", key), ("cpay", payload)],
        }
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="kns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    return plan, graph


def _run_many(plan: Any, sources: dict[str, pa.Table]) -> dict[str, pa.Table]:
    res = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=_GRAPH,
        namespace_registry=_NSREG,
    )
    return dict(res.outputs)


class TestIdenticalColumnsFixed:
    N = 200

    def _src(self, *cols: str) -> pa.Table:
        return pa.table({c: [f"v{i}" for i in range(self.N)] for c in cols})

    def test_two_namespaceless_columns_in_one_table_differ(self) -> None:
        plan = _multi_plan({"t": [("a", _seed()), ("b", _seed())]})
        out = _run_many(plan, {"t": self._src("a", "b")})["t"]
        a, b = out.column("a").to_pylist(), out.column("b").to_pylist()
        assert a != b
        pool = _pool(_seed())
        assert a == _expected(pool, _default_ns("t", "a"), range(self.N))
        assert b == _expected(pool, _default_ns("t", "b"), range(self.N))

    def test_the_same_column_name_in_two_tables_differs(self) -> None:
        plan = _multi_plan({"t1": [("c", _seed())], "t2": [("c", _seed())]})
        out = _run_many(plan, {"t1": self._src("c"), "t2": self._src("c")})
        assert out["t1"].column("c").to_pylist() != out["t2"].column("c").to_pylist()

    def test_two_columns_sharing_an_explicit_namespace_share_a_stream(self) -> None:
        # Documented: an explicit namespace is the stream key, so a shared one is shared.
        plan = _multi_plan(
            {"t": [("a", _seed(namespace="shared")), ("b", _seed(namespace="shared"))]}
        )
        out = _run_many(plan, {"t": self._src("a", "b")})["t"]
        assert out.column("a").to_pylist() == out.column("b").to_pylist()


# ---- 4. nulls ---------------------------------------------------------------------------


class TestNulls:
    def test_nulls_are_restored_by_position_and_consume_their_ordinal(self) -> None:
        base = _run(["x"] * 12)
        values: list[Any] = ["x"] * 12
        values[3] = None
        values[7] = float("nan")
        out = _run(values)
        assert out[3] is None and out[7] is None
        assert [o for i, o in enumerate(out) if i not in (3, 7)] == [
            b for i, b in enumerate(base) if i not in (3, 7)
        ]

    def test_row_k_is_unchanged_whether_or_not_row_k_minus_1_is_null(self) -> None:
        with_null = _run(["x", None, "x", "x"])
        without = _run(["x", "x", "x", "x"])
        assert with_null[2:] == without[2:]

    def test_all_null_and_empty_inputs(self) -> None:
        assert _run([None, None, None]) == [None, None, None]
        assert _run([]) == []


# ---- 5. the pool is unchanged -----------------------------------------------------------


class TestPoolUnchanged:
    @pytest.mark.parametrize("namespace", [None, "", "explicit"], ids=["none", "empty", "named"])
    def test_pool_is_built_with_the_original_plan_namespace(
        self, namespace: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        real = PoolBuilder.build

        def spy(self: PoolBuilder, **kwargs: Any) -> ValuePool:
            calls.append(dict(kwargs))
            return real(self, **kwargs)

        monkeypatch.setattr(PoolBuilder, "build", spy)
        seed = _seed(namespace=namespace)
        ctx = _ctx()
        _run(["x"] * 8, seed, ctx=ctx)
        assert len(calls) == 1
        assert calls[0]["namespace"] == namespace
        assert calls[0]["job_seed"] == JOB
        assert calls[0]["size"] == POOL
        built = _pool(seed)
        assert list(ctx.pool_cache.get(built.identity).values) == list(built.values)  # type: ignore[union-attr]

    def test_pool_identity_in_the_cache_uses_the_original_namespace(self) -> None:
        seed = _seed(namespace="explicit")
        ctx = _ctx()
        _run(["x"] * 4, seed, ctx=ctx)
        assert ctx.pool_cache.get(_pool(seed).identity) is not None
        # No pool was cached under the selection namespace's identity.
        other = PoolBuilder(_REG).identity_for(
            "person_first_name",
            size=POOL,
            job_seed=JOB,
            locale=None,
            config={},
            namespace=_default_ns("t", "c"),
        )
        assert ctx.pool_cache.get(other) is None

    def test_namespaceless_pool_equals_the_pre_change_pool(self) -> None:
        # The pre-change handler built the pool with `namespace=None` for this plan.
        seed = _seed()
        ctx = _ctx()
        _run(["x"] * 4, seed, ctx=ctx)
        pre_change = PoolBuilder(_REG).build(
            provider="person_first_name",
            size=POOL,
            job_seed=JOB,
            locale=None,
            config={},
            namespace=None,
        )
        cached = ctx.pool_cache.get(pre_change.identity)
        assert cached is not None
        assert list(cached.values) == list(pre_change.values)  # type: ignore[union-attr]

    def test_two_columns_share_one_pool_but_draw_different_rows(self) -> None:
        pool = _pool(_seed())
        a = _run(["x"] * 100, column="a")
        b = _run(["x"] * 100, column="b")
        assert set(a) <= set(pool.values.tolist()) and set(b) <= set(pool.values.tolist())
        assert a != b


# ---- 6. the other modes and deterministic Faker are unchanged ---------------------------


class TestOtherModesUnchanged:
    SRC = [f"s{i % 5}" for i in range(10)]

    def _legacy(self, seed: ColumnSeed, source: list[Any]) -> list[Any]:
        """The pre-change handler body for these modes: `PoolSampler` straight from job_seed."""
        sampled = PoolSampler().sample(
            _pool(seed),
            len(source),
            mode=CardinalityMode(seed.cardinality_mode),
            seed=JOB,
            source=pd.Series(source),
            namespace=seed.namespace,
            deterministic=False,
            scale=seed.scale if seed.scale is not None else 2.0,
        )
        return list(sampled)

    def test_unique_is_the_whole_column_numpy_draw(self) -> None:
        src = [f"s{i}" for i in range(10)]
        seed = _seed(mode="unique", namespace="ns")
        assert _run(src, seed) == self._legacy(seed, src)

    def test_match_source_cardinality_is_the_whole_column_numpy_draw(self) -> None:
        seed = _seed(mode="match_source_cardinality", namespace="ns")
        assert _run(self.SRC, seed) == self._legacy(seed, self.SRC)

    def test_scale_source_cardinality_is_the_whole_column_numpy_draw(self) -> None:
        seed = _seed(mode="scale_source_cardinality", namespace="ns", scale=0.5)
        got = _run(self.SRC, seed)
        assert got == self._legacy(seed, self.SRC)
        assert len(set(got)) == 2

    def test_unique_and_match_ignore_the_row_offset_and_table(self) -> None:
        src = [f"s{i}" for i in range(10)]
        seed = _seed(mode="unique", namespace="ns")
        assert _run(src, seed, ctx=_ctx(table="other", row_offset=999)) == _run(src, seed)

    def test_non_deterministic_unique_does_not_need_a_current_table(self) -> None:
        src = [f"s{i}" for i in range(10)]
        assert _run(src, _seed(mode="unique", namespace="ns"), ctx=_ctx(table="")) == _run(
            src, _seed(mode="unique", namespace="ns")
        )

    def test_deterministic_reuse_is_value_keyed_on_mask_key(self) -> None:
        seed = _seed(deterministic=True, namespace="ns")
        src = ["a", None, "b", "a"]
        pool = _pool(seed)
        expected = [
            None
            if v is None
            else pool.values[
                derive_index(MASK, "ns", kernel.canonicalize_derive_source(v), pool_size=pool.size)
            ]
            for v in src
        ]
        got = _run(src, seed, ctx=_ctx(mask_key=MASK))
        assert got == expected
        assert got[0] == got[3]
        # Deterministic Faker never needs the table and ignores row_offset.
        assert _run(src, seed, ctx=_ctx(table="", row_offset=77, mask_key=MASK)) == expected


# ---- 7. generation is unchanged -----------------------------------------------------------


class TestGenerationUnchanged:
    def test_pool_sampler_non_deterministic_reuse_is_the_frozen_numpy_stream(self) -> None:
        values = np.array([f"v{i}" for i in range(64)], dtype=object)
        pool = ValuePool(
            values=values,
            provider="p",
            locale="default",
            config_hash="h",
            seed=b"test-see",
            size=64,
            build_time_ms=0.0,
            backend_type="faker",
            backend_version="0",
            distinct_count=64,
        )
        got = PoolSampler().sample(
            pool, 12, mode=CardinalityMode.REUSE, seed=JOB, deterministic=False
        )
        assert [int(v[1:]) for v in got.tolist()] == [58, 0, 46, 5, 36, 52, 62, 37, 58, 3, 40, 29]

    def test_sample_bundle_non_deterministic_is_the_frozen_numpy_stream(self) -> None:
        from decoy_engine.generation.composite._bundle_pool import BundlePool

        arr = np.empty(8, dtype=object)
        for i in range(8):
            arr[i] = (f"a{i}", f"b{i}")
        pool = BundlePool(
            values=arr,
            provider="tc",
            locale="default",
            config_hash="h",
            seed=b"test-see",
            size=8,
            build_time_ms=0.0,
            backend_type="faker",
            backend_version="0",
            distinct_count=8,
            output_columns=("x", "y"),
        )
        out = PoolSampler().sample_bundle(
            pool, 10, mode=CardinalityMode.REUSE, seed=JOB, deterministic=False
        )
        assert out["x"].tolist() == ["a7", "a0", "a5", "a0", "a4", "a6", "a7", "a4", "a7", "a0"]
        assert out["y"].tolist() == ["b7", "b0", "b5", "b0", "b4", "b6", "b7", "b4", "b7", "b0"]

    def test_generation_build_and_sample_is_the_numpy_stream_over_its_own_pool(self) -> None:
        from decoy_engine.generation import _faker_pool
        from decoy_engine.generators.derivation import GenDeriveContext

        col = {
            "name": "v",
            "type": "faker",
            "faker_type": "city",
            "locale": "en_US",
            "faker_kwargs": {},
        }
        gen_ctx = GenDeriveContext.for_column(derive_key=None, column_config=col, fallback_seed=7)
        got = _faker_pool.build_and_sample(
            faker_type="city",
            faker_kwargs={},
            n=10,
            gen_ctx=gen_ctx,
            effective_locale="en_US",
            pool_size=64,
        )
        build_seed = gen_ctx.family_bytes(_faker_pool._BUILD_FAMILY)[:8]
        selection_seed = gen_ctx.family_bytes(_faker_pool._SELECTION_FAMILY)[:8]
        pool_values, _exact, _custom = _faker_pool.build_pool_values(
            "city", "en_US", build_seed, 64, {}
        )
        idx = np.random.default_rng(int.from_bytes(selection_seed, "big")).integers(0, 64, 10)
        assert got == [pool_values[i] for i in idx]


# ---- 8. routing is held constant ----------------------------------------------------------


def _route_config(
    tmp_path: Any, mode: str, namespace: str | None, *, pool_size: int | None = 400
) -> tuple[Any, Any]:
    from tests.unit.execution import _auto_chunk_support as support
    from tests.unit.execution import _multi_table_support as mt

    col: dict[str, Any] = {
        "name": "f",
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": False,
        "cardinality_mode": mode,
    }
    if pool_size is not None:
        col["pool_size"] = pool_size
    if namespace:
        col["namespace"] = namespace
    table = pa.table(
        {"f": [f"n{i}" for i in range(40)], "h": [f"h{i}" for i in range(40)]},
    )
    return mt.build_job(tmp_path, {"t": ([col, support.hash_col("h", "hn")], table)})


# (mode, namespace) -> the chunked conditions that stay unmet. Snapshot of engine main 4fe5de9d.
# The REUSE rows left this table when C5b-ii admitted a REUSE column that declares a pool_size
# to the chunked route; `_REUSE_WITHOUT_POOL_SIZE` keeps the rest of that routing pinned.
_ROUTE_CASES = [
    ("unique", "ns_f", 2),
    ("match_source_cardinality", "ns_f", 2),
    ("scale_source_cardinality", None, 3),
]
_REUSE_WITHOUT_POOL_SIZE = [None, "ns_f"]


class TestRoutingConstant:
    @pytest.mark.parametrize(("mode", "namespace", "unmet"), _ROUTE_CASES)
    def test_planner_decision_and_reason_codes_equal_main(
        self, tmp_path: Any, mode: str, namespace: str | None, unmet: int
    ) -> None:
        from decoy_engine.execution._planner import classify_job
        from decoy_engine.plan import compile_plan
        from decoy_engine.profile import profile_source

        cfg, src = _route_config(tmp_path, mode, namespace)
        plan = compile_plan(
            cfg, profile_source(cfg, seed=42), decoy_engine_version="c5b-i-route-test"
        )
        decision = classify_job(
            cfg,
            plan=plan,
            registry=_REG,
            relationship_graph=_GRAPH,
            substrate="pandas",
            source_tables=src,
            auto_chunk_threshold_rows=10,
        )
        assert decision.mode == "pandas_fallback"
        assert set(decision.rejections) == {
            "chunked",
            "sequential_relationship",
            "out_of_core_relationship",
        }
        chunked = decision.rejections["chunked"]
        assert chunked.startswith("chunked_strategy_conditions_unmet: column(s) f (faker: ")
        head = chunked.split(". faker/categorical", 1)[0]
        assert head.count("requires ") == unmet
        assert "requires deterministic: true" in head

    @pytest.mark.parametrize("namespace", _REUSE_WITHOUT_POOL_SIZE)
    def test_a_reuse_column_without_a_pool_size_keeps_the_veto_and_runs_full_frame(
        self, tmp_path: Any, namespace: str | None
    ) -> None:
        from decoy_engine.execution import run_pipeline
        from decoy_engine.execution._planner import classify_job
        from decoy_engine.plan import compile_plan
        from decoy_engine.profile import profile_source
        from tests.unit.execution import _multi_table_support as mt

        cfg, src = _route_config(tmp_path, "reuse", namespace, pool_size=None)
        plan = compile_plan(
            cfg, profile_source(cfg, seed=42), decoy_engine_version="c5b-ii-route-test"
        )
        decision = classify_job(
            cfg,
            plan=plan,
            registry=_REG,
            relationship_graph=_GRAPH,
            substrate="pandas",
            source_tables=src,
            auto_chunk_threshold_rows=10,
        )
        assert decision.mode == "pandas_fallback"
        assert decision.rejections["chunked"].startswith(
            "chunked_strategy_conditions_unmet: column(s) f (faker: "
        )
        res = run_pipeline(cfg, sources=src, **mt.kw())
        block = res.quality_metrics["auto_chunk"]
        assert block["mode"] == "full_frame"
        assert block["reason"].startswith("chunked_strategy_conditions_unmet")

    @pytest.mark.parametrize(("mode", "namespace", "unmet"), _ROUTE_CASES[:2])
    def test_end_to_end_run_stays_full_frame_with_the_same_code(
        self, tmp_path: Any, mode: str, namespace: str | None, unmet: int
    ) -> None:
        from decoy_engine.execution import run_pipeline
        from tests.unit.execution import _multi_table_support as mt

        del unmet
        cfg, src = _route_config(tmp_path, mode, namespace)
        res = run_pipeline(cfg, sources=src, **mt.kw())
        block = res.quality_metrics["auto_chunk"]
        assert block["mode"] == "full_frame"
        assert block["reason"].startswith("chunked_strategy_conditions_unmet")
        assert set(res.quality_metrics) == {"auto_chunk", "execution"}

    @pytest.mark.parametrize("mode", ["unique", "match_source_cardinality"])
    def test_chunked_check_still_raises_the_same_code(self, mode: str) -> None:
        from decoy_engine.execution._chunked import check_chunked_compatibility
        from decoy_engine.plan._errors import PlanCompileError
        from tests.native._chunked_entry_support import make_config

        col = {
            "name": "f",
            "strategy": "faker",
            "provider": "person_first_name",
            "deterministic": False,
            "namespace": "ns_f",
            "pool_size": 400,
            "cardinality_mode": mode,
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(make_config([col]), table="t", registry=_REG)
        assert exc.value.code == "chunked_strategy_conditions_unmet"

    def test_out_of_core_still_rejects_every_faker_with_the_same_code(self) -> None:
        from decoy_engine.execution._runner import build_work_list, order_work
        from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility

        for det in (False, True):
            plan, graph = _fk_plan(_seed(namespace="ns", deterministic=det))
            work = order_work(build_work_list(plan, _REG), graph)
            compat = check_out_of_core_compatibility(plan, work, graph)
            assert not compat.accepted
            assert [r.code for r in compat.rejections] == ["out_of_core_faker_pool_unsupported"] * 2

    @pytest.mark.parametrize(
        "mode", ["reuse", "unique", "match_source_cardinality", "scale_source_cardinality"]
    )
    def test_native_pool_admission_outcome_equals_main(self, mode: str) -> None:
        from decoy_engine.execution.native._requirements import (
            faker_pool_precondition_met,
            native_pool_rejection,
        )

        nondet = SimpleNamespace(plan_slice=_seed(namespace="ns", mode=mode, plan_pool_size=100))
        assert faker_pool_precondition_met(nondet) is False
        assert (
            native_pool_rejection(nondet, "c", "faker") == "faker_not_deterministic_reuse_variant:c"
        )
        det = SimpleNamespace(
            plan_slice=_seed(namespace="ns", mode=mode, plan_pool_size=100, deterministic=True)
        )
        assert faker_pool_precondition_met(det) is (mode == "reuse")

    def test_multi_table_vetoes_still_do_not_name_faker(self) -> None:
        from decoy_engine.execution import _pipeline_multi_table as pmt

        plan = _multi_plan({"t": [("c", _seed()), ("d", _seed(mode="unique", namespace="n"))]})
        assert "faker" not in pmt.UNSEEDED_RANDOM_STRATEGIES
        assert "faker" not in pmt.POSITION_KEYED_CATEGORICAL_SPLIT_DEFERRED
        assert pmt.unseeded_random_nodes(plan) == ()
        assert pmt.position_keyed_deferred_nodes(plan) == ()


# ---- 9 is in tests/native/test_c5b_i_metadata_inventory.py ----------------------------------


# ---- 10. the per-column default namespace comes from the context -----------------------------


class TestDefaultNamespaceFromContext:
    def test_helper_builds_the_length_prefixed_default(self) -> None:
        assert fp.faker_selection_namespace("t", "c", None) == "faker-nd/1:t/1:c"
        assert fp.faker_selection_namespace("t", "c", "") == "faker-nd/1:t/1:c"
        assert fp.faker_selection_namespace("t", "c", "mine") == "mine"

    def test_the_default_uses_the_table_and_column(self) -> None:
        seed = _seed()
        pool = _pool(seed)
        got = _run(["x"] * 20, seed, ctx=_ctx(table="patients"), column="first")
        assert got == _expected(pool, "faker-nd/8:patients/5:first", range(20))

    def test_path_like_names_cannot_collide(self) -> None:
        a = fp.faker_selection_namespace("exports/customer", "name", None)
        b = fp.faker_selection_namespace("exports", "customer/name", None)
        assert a == "faker-nd/16:exports/customer/4:name"
        assert b == "faker-nd/7:exports/13:customer/name"
        assert a != b
        pool = _pool(_seed())
        out_a = _run(["x"] * 30, ctx=_ctx(table="exports/customer"), column="name")
        out_b = _run(["x"] * 30, ctx=_ctx(table="exports"), column="customer/name")
        assert out_a == _expected(pool, a, range(30))
        assert out_b == _expected(pool, b, range(30))
        assert out_a != out_b

    def test_empty_string_namespace_behaves_exactly_like_none(self) -> None:
        none_out = _run(["x"] * 40, _seed(namespace=None))
        empty_out = _run(["x"] * 40, _seed(namespace=""))
        assert empty_out == none_out
        # And the pool is today's: built with the empty namespace, not the selection default.
        empty_pool = _pool(_seed(namespace=""))
        assert empty_out == _expected(empty_pool, "faker-nd/1:t/1:c", range(40))

    def test_empty_current_table_is_a_hard_error(self) -> None:
        with pytest.raises(StrategyError) as exc:
            _run(["x"] * 3, ctx=_ctx(table=""))
        assert exc.value.code == "faker_positional_table_unknown"

    def test_a_context_without_current_table_is_a_hard_error(self) -> None:
        class _Bare:
            job_seed = JOB
            mask_key = JOB
            row_offset = 0
            registry = _REG
            pool_cache = PoolCache()

        with pytest.raises(StrategyError) as exc:
            FakerStrategyHandler().run(
                pd.DataFrame({"c": ["x"]}),
                "c",
                _seed(),
                _Bare(),  # type: ignore[arg-type]
            )
        assert exc.value.code == "faker_positional_table_unknown"

    def test_an_explicit_namespace_does_not_need_a_table(self) -> None:
        seed = _seed(namespace="people")
        assert _run(["x"] * 12, seed, ctx=_ctx(table="")) == _run(["x"] * 12, seed)


class TestNestedFaker:
    CELLS: list[Any] = [
        json.dumps({"items": [{"k": "a"}, {"k": "b"}, {"k": "c"}]}),
        json.dumps({"other": 1}),  # sparse: no matching leaf
        None,
        json.dumps({"items": [{"k": "d"}, {"k": None}, {"k": "e"}]}),  # null leaf
        json.dumps({"items": [{"k": "f"}]}),
    ]

    def _nested(self) -> ColumnSeed:
        return _seed(
            strategy="nested",
            config=(
                ("strategy", "faker"),
                ("strategy_config", {"pool_size": POOL}),
                ("target", "$.items[*].k"),
            ),
        )

    def _run_nested(self, column: str, cells: list[Any] | None = None) -> list[str | None]:
        df = pd.DataFrame({column: list(self.CELLS if cells is None else cells)})
        out, _ = NestedStrategyHandler().run(df, column, self._nested(), _ctx())
        leaves: list[str | None] = []
        for cell in out[column].tolist():
            if cell is None:
                continue
            leaves.extend(item["k"] for item in json.loads(cell).get("items", []))
        return leaves

    def test_leaves_use_the_outer_column_in_the_default_and_flattened_ordinals(self) -> None:
        pool = _pool(_seed())
        got = self._run_nested("data")
        expected = _expected(pool, _default_ns("t", "data"), range(7))
        expected[4] = None  # the null leaf keeps its ordinal and stays null
        assert got == expected

    def test_two_namespaceless_nested_columns_in_one_table_differ(self) -> None:
        assert self._run_nested("data1") != self._run_nested("data2")

    def test_the_synthetic_leaf_column_is_not_the_default(self) -> None:
        pool = _pool(_seed())
        got = self._run_nested("data")
        assert got != _expected(pool, _default_ns("t", "_nested_leaves"), range(7))

    def test_leaf_values_do_not_influence_the_draw(self) -> None:
        a = self._run_nested("data", [json.dumps({"items": [{"k": "a"}, {"k": "b"}]})])
        b = self._run_nested("data", [json.dumps({"items": [{"k": "zzz"}, {"k": "yyy"}]})])
        assert a == b


# ---- 11. offsets, frames, kernels -------------------------------------------------------------


class TestOffsetsAndFrames:
    def test_nonzero_row_offset_default_namespace(self) -> None:
        pool = _pool(_seed())
        out = _run(["x"] * 8, ctx=_ctx(row_offset=1000))
        assert out == [pool.values[i] for i in KAT_DEFAULT_1000]

    def test_nonzero_row_offset_explicit_namespace(self) -> None:
        seed = _seed(namespace="people")
        pool = _pool(seed)
        out = _run(["x"] * 8, seed, ctx=_ctx(row_offset=1000))
        assert out == [pool.values[i] for i in KAT_PEOPLE_1000]

    def test_nonzero_offset_on_the_reference_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fp, "_compiled_index_kernel", lambda: None)
        pool = _pool(_seed())
        assert _run(["x"] * 8, ctx=_ctx(row_offset=1000)) == [
            pool.values[i] for i in KAT_DEFAULT_1000
        ]

    @NEEDS_COMPANION
    def test_nonzero_offset_on_the_compiled_path(self) -> None:
        pool = _pool(_seed())
        assert _run(["x"] * 8, ctx=_ctx(row_offset=1000)) == [
            pool.values[i] for i in KAT_DEFAULT_1000
        ]

    def test_batch_kernels_on_the_global_index_column_equal_the_scalar_indices(self) -> None:
        ks: list[Any] = [reference_index_derivation()]
        if native_companion_status().ok:
            ks.append(load_compiled_index_kernel())
        keys = pa.array(np.uint64(1000) + np.arange(8, dtype=np.uint64), pa.uint64())
        for k in ks:
            got = k.derive_index_batch(
                keys, mask_key=JOB, namespace=_default_ns("t", "c"), pool_size=POOL
            )
            assert got.to_pylist() == KAT_DEFAULT_1000

    def test_the_key_is_job_seed_not_mask_key(self) -> None:
        pool = _pool(_seed())
        out = _run(["x"] * 30, ctx=_ctx(job_seed=JOB, mask_key=MASK))
        assert out == _expected(pool, _default_ns("t", "c"), range(30), key=JOB)
        assert out != _expected(pool, _default_ns("t", "c"), range(30), key=MASK)

    def test_the_offset_domain_is_checked(self) -> None:
        from decoy_engine.generation.pool import GenerationError

        with pytest.raises(GenerationError) as exc:
            _run(["x", "y"], ctx=_ctx(row_offset=2**64 - 1))
        assert exc.value.code == "faker_position_out_of_domain"
        # The last representable ordinal is accepted.
        assert len(_run(["x"], ctx=_ctx(row_offset=2**64 - 1))) == 1

    def test_when_gate_keys_by_match_ordinal_and_leaves_unmatched_rows_alone(self) -> None:
        df = pd.DataFrame(
            {"col": [f"v{i}" for i in range(6)], "keep": [0, 1, 0, 1, 1, 0]},
        )
        seed = _seed(when="keep == 1")
        out, _ = run_with_when_gate(FakerStrategyHandler(), df, "col", seed, _ctx())
        got = out["col"].tolist()
        pool = _pool(_seed())
        exp = _expected(pool, _default_ns("t", "col"), range(3))
        assert [got[1], got[3], got[4]] == exp
        assert [got[i] for i in (0, 2, 5)] == ["v0", "v2", "v5"]

    def test_orphan_remap_frame_keys_by_synthetic_frame_ordinal_and_parent_identity(
        self,
    ) -> None:
        pseed = _seed()
        node = SimpleNamespace(plan_slice=pseed, strategy="faker")
        edge = SimpleNamespace(parent_table="p", parent_columns=("pk",))
        ctx = _ctx(table="child")
        remap = make_remap_fn(
            edge,  # type: ignore[arg-type]
            {("p", ("pk",)): node},  # type: ignore[dict-item]
            ctx,
            {"faker": FakerStrategyHandler()},
        )
        keys = [("o1",), ("o2",), ("o3",), ("o4",)]
        out = remap(keys)
        assert out == remap(keys)
        pool = _pool(pseed)
        assert [k[0] for k in out] == _expected(pool, _default_ns("p", "pk"), range(4))
        assert ctx.current_table == "child"  # restored

    @NEEDS_COMPANION
    def test_the_compiled_batch_kernel_is_actually_called(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inner = load_compiled_index_kernel()

        class _Spy:
            calls = 0

            def derive_index_batch(self, *args: Any, **kwargs: Any) -> Any:
                type(self).calls += 1
                return inner.derive_index_batch(*args, **kwargs)

        monkeypatch.setattr(fp, "_compiled_index_kernel", lambda: _Spy())
        pool = _pool(_seed())
        out = _run(["x"] * 25)
        assert _Spy.calls == 1  # one batch call for the whole column, not per row
        assert out == _expected(pool, _default_ns("t", "c"), range(25))

    def test_the_reference_kernel_is_used_when_the_companion_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inner = reference_index_derivation()

        class _Spy:
            calls = 0

            def derive_index_batch(self, *args: Any, **kwargs: Any) -> Any:
                type(self).calls += 1
                return inner.derive_index_batch(*args, **kwargs)

        monkeypatch.setattr(fp, "_compiled_index_kernel", lambda: None)
        monkeypatch.setattr(fp, "_reference_index_kernel", lambda: _Spy())
        _run(["x"] * 25)
        assert _Spy.calls == 1


# ---- 12. multi-table: a sibling stays split-eligible ---------------------------------------------


class TestMultiTable:
    def _job(self, tmp_path: Any) -> tuple[Any, Any]:
        from tests.unit.execution import _multi_table_support as mt

        faker_col: dict[str, Any] = {
            "name": "f",
            "strategy": "faker",
            "provider": "person_first_name",
            "deterministic": False,
            "pool_size": 400,
        }
        n = mt.BIG
        faker_tbl = pa.table({"f": [f"n{i}" for i in range(n)], "h": [f"h{i}" for i in range(n)]})
        from tests.unit.execution import _auto_chunk_support as support

        return mt.build_job(
            tmp_path,
            {
                "fk": ([faker_col, support.hash_col("h", "hn")], faker_tbl),
                "big": (mt.std_columns("big_ns"), mt.string_table(mt.BIG, "b")),
                "big2": (mt.std_columns("big2_ns"), mt.string_table(mt.BIG, "c")),
            },
        )

    def test_sibling_tables_still_dispatch_and_the_faker_table_runs_chunked_on_the_same_ordinals(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from decoy_engine.execution import run_pipeline
        from tests.unit.execution import _multi_table_support as mt

        cfg, src = self._job(tmp_path)
        calls = mt.spy_split(monkeypatch)
        got = run_pipeline(cfg, sources=src, **mt.kw())
        assert len(calls) == 1
        # C5b-ii: the above-threshold Faker table is dispatched like its siblings.
        assert mt.dispatched_tables(got) == ["fk", "big", "big2"]
        off = run_pipeline(cfg, sources=src, **mt.kw(**mt.off_kw()))
        # Dispatched tables differ from the whole-frame run only in pandas schema metadata.
        for name in ("fk", "big", "big2"):
            assert got.outputs[name].equals(off.outputs[name]), name
        # The Faker table drew from the whole-frame ordinals, row for row.
        job_seed = _job_seed_of(cfg)
        pool = _pool(_seed(config=(("pool_size", 400),)), job_seed)
        values = got.outputs["fk"].column("f").to_pylist()
        assert values == _expected(
            pool,
            _default_ns("fk", "f"),
            range(len(values)),
            key=job_seed,
        )


def _job_seed_of(cfg: dict[str, Any]) -> bytes:
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import profile_source

    plan = compile_plan(cfg, profile_source(cfg, seed=42), decoy_engine_version="c5b-i-test")
    return plan.seed_envelope.job_seed
