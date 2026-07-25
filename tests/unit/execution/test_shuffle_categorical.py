"""engine-v2 S9 slice 2e: shuffle (derive-seeded rng) + categorical (pool remap)."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, ExecutionResult, PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0x77).to_bytes(8, "big")


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    deterministic: bool = False,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(col_name: str, seed: ColumnSeed) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("t", TableSeed(per_column=((col_name, seed),), per_group=())),),
        )
    )


def _run(plan: Any, table: pa.Table) -> ExecutionResult:
    return PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )


class _Ctx:
    """Minimal StrategyContext stand-in for direct handler.run() calls (the
    pandas-level dtype/index behavior is only observable before the Arrow
    conversion the pipeline applies)."""

    job_seed = _SEED
    mask_key = _SEED


def _direct_seed() -> ColumnSeed:
    return _col("shuffle", namespace="sh", deterministic=True)


class TestShuffle:
    def test_preserves_multiset_and_nulls_deterministic(self) -> None:
        src = pa.table({"c": ["a", "b", "c", None, "a"]})
        seed = _col("shuffle", namespace="sh", deterministic=True)
        out = _run(_plan("c", seed), src).output.column("c").to_pylist()
        assert out[3] is None  # null preserved in place
        # multiset of non-null values is preserved (it's a permutation)
        assert Counter(v for v in out if v is not None) == Counter(["a", "b", "c", "a"])

    def test_deterministic_reproducible(self) -> None:
        src = pa.table({"c": ["a", "b", "c", "d", "e"]})
        seed = _col("shuffle", namespace="sh", deterministic=True)
        out1 = _run(_plan("c", seed), src).output.column("c").to_pylist()
        out2 = _run(_plan("c", seed), src).output.column("c").to_pylist()
        assert out1 == out2

    def test_same_namespace_distinct_columns_diverge(self) -> None:
        # F4 regression: two deterministic-shuffle columns in the SAME namespace
        # must NOT receive the identical permutation. The derivation source binds
        # the column name; without it both columns permute in lockstep and re-link
        # values that masking was meant to decouple (a privacy failure).
        values = list("abcdefghij")  # 10 distinct -> collision odds 1/10! negligible
        seed = _col("shuffle", namespace="pii", deterministic=True)
        out_a = _run(_plan("a", seed), pa.table({"a": values})).output.column("a").to_pylist()
        out_b = _run(_plan("b", seed), pa.table({"b": values})).output.column("b").to_pylist()
        assert out_a != out_b

    def test_deterministic_requires_namespace(self) -> None:
        src = pa.table({"c": ["a", "b"]})
        with pytest.raises(ExecutionError) as exc:
            _run(_plan("c", _col("shuffle", namespace=None, deterministic=True)), src)
        assert exc.value.code == "shuffle_requires_namespace"
        assert exc.value.strategy == "shuffle"

    def test_non_deterministic_shuffle_runs_and_preserves_multiset(self) -> None:
        # The unseeded rng branch (deterministic=False) is a distinct code path;
        # a mutant that nulls that rng would crash here. No namespace needed.
        src = pa.table({"c": ["a", "b", "c", "d", "e", None]})
        out = _run(_plan("c", _col("shuffle", deterministic=False)), src).output.column("c").to_pylist()
        assert out[5] is None
        assert Counter(v for v in out if v is not None) == Counter(["a", "b", "c", "d", "e"])

    def test_deterministic_permutation_is_the_pinned_known_answer(self) -> None:
        # Pins the exact derive-seeded permutation, so a mutated seed slice
        # (derive(...)[:8] -> [:9]) or a nulled deterministic rng produces a
        # different order and fails here. Computed once from the real handler.
        src = pa.table({"c": ["a", "b", "c", "d", "e"]})
        seed = _col("shuffle", namespace="sh", deterministic=True)
        out = _run(_plan("c", seed), src).output.column("c").to_pylist()
        assert out == ["d", "e", "c", "a", "b"]

    def test_output_keeps_object_dtype_and_null_not_nan(self) -> None:
        # Q13: an int column widens to float64 in pandas once it carries a null;
        # the handler wraps the output in an explicit object-dtype Series so the
        # assignment does not re-infer float64 (which would turn the null into
        # NaN). Direct handler call so the pandas dtype is observable pre-Arrow.
        from decoy_engine.execution._strategies._shuffle import ShuffleStrategyHandler

        df = pd.DataFrame({"n": [10, 20, 30, 40, 50]})  # pure int64, no null
        out, _ = ShuffleStrategyHandler().run(df.copy(), "n", _direct_seed(), _Ctx())
        assert out["n"].dtype == object  # a dropped dtype=object re-infers int64

    def test_non_default_index_preserved_and_aligned(self) -> None:
        # The output Series must carry the source index; a RangeIndex (index=None
        # or dropped) misaligns against a non-default-index frame on assignment
        # and blanks every row to NaN.
        from decoy_engine.execution._strategies._shuffle import ShuffleStrategyHandler

        df = pd.DataFrame({"c": ["a", "b", "c", "d", "e"]}, index=[10, 20, 30, 40, 50])
        out, _ = ShuffleStrategyHandler().run(df.copy(), "c", _direct_seed(), _Ctx())
        assert list(out.index) == [10, 20, 30, 40, 50]
        assert Counter(out["c"]) == Counter(["a", "b", "c", "d", "e"])  # no NaN blanks


class TestCategorical:
    def test_remaps_into_categories_deterministic(self) -> None:
        src = pa.table({"grade": ["x", "y", "x", None]})
        seed = _col(
            "categorical",
            namespace="g",
            deterministic=True,
            provider_config=(("categories", ["A", "B", "C"]),),
        )
        out = _run(_plan("grade", seed), src).output.column("grade").to_pylist()
        assert out[3] is None
        assert all(v in {"A", "B", "C"} for v in out if v is not None)
        assert out[0] == out[2]  # same source -> same category

    def test_reproducible(self) -> None:
        src = pa.table({"grade": ["x", "y", "z"]})
        seed = _col(
            "categorical",
            namespace="g",
            deterministic=True,
            provider_config=(("categories", ["A", "B", "C", "D"]),),
        )
        out1 = _run(_plan("grade", seed), src).output.column("grade").to_pylist()
        out2 = _run(_plan("grade", seed), src).output.column("grade").to_pylist()
        assert out1 == out2

    def test_requires_categories(self) -> None:
        src = pa.table({"grade": ["x"]})
        with pytest.raises(ExecutionError) as exc:
            _run(_plan("grade", _col("categorical", namespace="g", deterministic=True)), src)
        assert exc.value.code == "categorical_requires_categories"

    def test_non_deterministic_is_unseeded(self) -> None:
        # M2: non-deterministic categorical is UNSEEDED (matches faker + shuffle),
        # so two runs with the same job seed differ. Many rows make this robust.
        src = pa.table({"grade": ["x"] * 200})
        seed = _col(
            "categorical", deterministic=False, provider_config=(("categories", list("ABCD")),)
        )
        out1 = _run(_plan("grade", seed), src).output.column("grade").to_pylist()
        out2 = _run(_plan("grade", seed), src).output.column("grade").to_pylist()
        assert out1 != out2
