"""C1b-i route-regression guard (plan section 5.7).

Making non-deterministic categorical seeded changes its determinism metadata, but no
routing outcome. Every route whose position-keyed implementation is deferred to C1b-ii
must stay closed, now under a truthful "position-keyed implementation deferred" reason
instead of the false "unseeded" label:

  a. multi-table split: a table with non-deterministic categorical (bare or nested) still
     does not split, and its sibling tables do not start splitting;
  b. out-of-core compatibility still rejects it, with the exact code and the new reason;
  c. automatic AND explicit out-of-core routes do not admit it, and the impl still raises;
  d. the chunked error code `categorical_nondeterministic_not_chunk_safe` is unchanged;
  e. native admission still declines `deterministic=False`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.errors import ConfigError
from decoy_engine.execution import _pipeline_multi_table as pmt
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.execution._pipeline_routing_signals import out_of_core_admission
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.native._categorical_prepared import prepare_categorical
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.execution.out_of_core._mask_group_b import categorical_array
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _multi_table_support as mt

pytestmark = pytest.mark.filterwarnings("ignore")

_REG = get_default_registry()
_JOB_SEED = (0x42).to_bytes(8, "big")
OOC_CODE = "out_of_core_categorical_nondeterministic_unsupported"
DEFERRED_REASON = "position-keyed implementation deferred (C1b-ii)"


def _cat_seed(*, deterministic: bool = False, nested: bool = False) -> ColumnSeed:
    cfg: tuple[tuple[str, Any], ...] = (("categories", ("A", "B", "C")),)
    if nested:
        cfg = (
            ("strategy", "categorical"),
            ("strategy_config", {"categories": ["A", "B", "C"]}),
            ("target", "$.k"),
        )
    return ColumnSeed(
        namespace="cat",
        strategy="nested" if nested else "categorical",
        provider="categorical",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=cfg,
        coherent_with=(),
    )


def _hash_seed() -> ColumnSeed:
    return ColumnSeed(
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


def _fk_job(payload: ColumnSeed) -> tuple[Any, RelationshipGraph]:
    plan: Any = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                (
                    "parent",
                    TableSeed(per_column=(("pk", _hash_seed()), ("pay", payload)), per_group=()),
                ),
                (
                    "child",
                    TableSeed(per_column=(("fk", _hash_seed()), ("cpay", payload)), per_group=()),
                ),
            ),
        )
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


def _single_table_plan(*seeds: ColumnSeed) -> Any:
    cols = tuple((f"c{i}", s) for i, s in enumerate(seeds))
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(("t", TableSeed(per_column=cols, per_group=())),),
        )
    )


# ---- a. multi-table split ------------------------------------------------------------


class TestMultiTableStaysWhole:
    def test_nondeterministic_categorical_is_no_longer_labelled_unseeded(self) -> None:
        plan = _single_table_plan(_cat_seed(), _cat_seed(nested=True))
        assert "categorical" not in pmt.UNSEEDED_RANDOM_STRATEGIES
        assert pmt.unseeded_random_nodes(plan) == ()

    def test_it_is_reported_by_the_truthful_positional_deferred_veto(self) -> None:
        plan = _single_table_plan(
            _cat_seed(), _cat_seed(nested=True), _cat_seed(deterministic=True)
        )
        assert frozenset({"categorical"}) == pmt.POSITION_KEYED_CATEGORICAL_SPLIT_DEFERRED
        assert pmt.position_keyed_deferred_nodes(plan) == (
            ("t", "c0", "categorical"),
            ("t", "c1", "nested"),
        )

    def test_a_deterministic_categorical_is_not_vetoed(self) -> None:
        plan = _single_table_plan(_cat_seed(deterministic=True))
        assert pmt.position_keyed_deferred_nodes(plan) == ()
        assert pmt.unseeded_random_nodes(plan) == ()

    def test_shuffle_is_still_unseeded_and_not_positional_deferred(self) -> None:
        shuffle = ColumnSeed(
            namespace=None,
            strategy="shuffle",
            provider="shuffle",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(),
            coherent_with=(),
        )
        plan = _single_table_plan(shuffle)
        assert pmt.unseeded_random_nodes(plan) == (("t", "c0", "shuffle"),)
        assert pmt.position_keyed_deferred_nodes(plan) == ()

    @pytest.mark.parametrize("kind", ["categorical", "nested"])
    def test_job_with_the_column_does_not_split_and_matches_split_off(
        self, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = kind == "nested"
        n = mt.SMALL * 40
        values = pa.table(
            {
                "v": pa.array(
                    [
                        f'{{"k": "{["red", "green", "blue"][i % 3]}"}}'
                        if nested
                        else ["red", "green", "blue"][i % 3]
                        for i in range(n)
                    ]
                ),
                "h": pa.array([f"u{i}" for i in range(n)]),
            }
        )
        if nested:
            col: dict[str, Any] = {
                "name": "v",
                "strategy": "nested",
                "deterministic": False,
                "namespace": "v_ns",
                "provider_config": {
                    "target": "$.k",
                    "strategy": "categorical",
                    "strategy_config": {"categories": ["x", "y", "z"]},
                },
            }
        else:
            col = {
                "name": "v",
                "strategy": "categorical",
                "deterministic": False,
                "namespace": "v_ns",
                "provider_config": {"categories": ["x", "y", "z"]},
            }
        cfg, sources = mt.build_job(
            tmp_path,
            {
                "tiny": ([col, support.hash_col("h", "r_ns")], values.slice(0, mt.SMALL)),
                "big": (mt.std_columns("big_ns"), mt.string_table(mt.BIG, "b")),
                "big2": (mt.std_columns("big2_ns"), mt.string_table(mt.BIG, "c")),
            },
        )
        calls = mt.spy_split(monkeypatch)
        adapter_calls = mt.spy_adapter_run(monkeypatch)
        got = run_pipeline(cfg, sources=sources, **mt.kw())
        assert calls == [], "the split executor must not run (siblings stay whole too)"
        assert len(adapter_calls) == 1
        off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
        assert list(got.outputs) == list(off.outputs)
        for name in got.outputs:
            assert got.outputs[name].equals(off.outputs[name], check_metadata=True), name
        assert mt.dispatched_tables(got) == []


# ---- b. out-of-core compatibility still rejects -------------------------------------------


class TestOutOfCoreStillRejects:
    @pytest.mark.parametrize("nested", [False])
    def test_compat_rejects_with_the_same_code_and_a_truthful_reason(self, nested: bool) -> None:
        plan, graph = _fk_job(_cat_seed(nested=nested))
        work = order_work(build_work_list(plan, _REG), graph)
        compat = check_out_of_core_compatibility(plan, work, graph)
        assert not compat.accepted
        rejections = [r for r in compat.rejections if r.code == OOC_CODE]
        assert len(rejections) == 2  # one per payload column
        for r in rejections:
            assert DEFERRED_REASON in r.message
            assert "unseeded" not in r.message.lower()
            assert "Falls back to full-frame" in r.message

    def test_deterministic_categorical_is_still_admitted(self) -> None:
        plan, graph = _fk_job(_cat_seed(deterministic=True))
        work = order_work(build_work_list(plan, _REG), graph)
        assert check_out_of_core_compatibility(plan, work, graph).accepted

    def test_the_out_of_core_impl_still_raises_for_nondeterministic(self) -> None:
        with pytest.raises(ExecutionError) as exc:
            categorical_array(
                pa.array(["a", "b"]),
                job_seed=_JOB_SEED,
                namespace="cat",
                deterministic=False,
                cfg={"categories": ["A", "B"]},
            )
        assert exc.value.code == OOC_CODE


# ---- c. automatic AND explicit out-of-core routes do not admit it ---------------------------


class _Profile:
    relationships = (object(),)


def _decide(plan: Any, graph: RelationshipGraph, **overrides: Any) -> tuple[str, str]:
    compatible, code = out_of_core_admission(plan, registry=_REG, graph=graph)
    kwargs: dict[str, Any] = {
        "has_generate_table": False,
        "has_mask_table": True,
        "validators": [],
        "fidelity_report": False,
        "vault_writer": None,
        "execution_mode": "auto",
        "graph": graph,
        "resolved_substrate": "pandas",
        "out_of_core_compatible": compatible,
        "out_of_core_reject_code": code,
        "largest_table_rows": 1_000_000,
        "out_of_core_threshold_rows": 100,
        "full_frame_reject_rows": 10_000_000,
        "use_byte_estimate_routing": False,
    }
    kwargs.update(overrides)
    return decide_execution_route(_Profile(), **kwargs)


class TestOutOfCoreRoutesStayClosed:
    def test_admission_signal_is_false_with_the_unchanged_code(self) -> None:
        plan, graph = _fk_job(_cat_seed())
        assert out_of_core_admission(plan, registry=_REG, graph=graph) == (False, OOC_CODE)

    def test_automatic_route_never_picks_out_of_core(self) -> None:
        plan, graph = _fk_job(_cat_seed())
        route, _reason = _decide(plan, graph)
        assert route == "sequential"
        route, _reason = _decide(plan, graph, use_byte_estimate_routing=True)
        assert route == "sequential"

    def test_explicit_out_of_core_is_refused_with_the_code(self) -> None:
        plan, graph = _fk_job(_cat_seed())
        with pytest.raises(ConfigError) as exc:
            _decide(plan, graph, execution_mode="out_of_core")
        assert OOC_CODE in str(exc.value)

    def test_control_the_deterministic_job_does_route_out_of_core(self) -> None:
        plan, graph = _fk_job(_cat_seed(deterministic=True))
        route, _reason = _decide(plan, graph)
        assert route == "out_of_core"
        route, _reason = _decide(plan, graph, execution_mode="out_of_core")
        assert route == "out_of_core"


# ---- d. chunked veto: code unchanged ------------------------------------------------------


class TestChunkedCodeUnchanged:
    def test_code_constant_and_path(self) -> None:
        from decoy_engine.execution import _chunked_categorical as cc
        from decoy_engine.plan._errors import PlanCompileError

        assert cc.NONDETERMINISTIC_CODE == "categorical_nondeterministic_not_chunk_safe"
        with pytest.raises(PlanCompileError) as exc:
            cc.reject_nondeterministic(["c"], table="t")
        assert exc.value.code == "categorical_nondeterministic_not_chunk_safe"
        assert exc.value.path == "tables.t.columns"
        assert "unseeded" not in str(exc.value).lower()
        assert "C1b-ii" in str(exc.value)

    def test_admission_outcome_is_unchanged_end_to_end(self) -> None:
        from decoy_engine.execution._chunked import check_chunked_compatibility
        from decoy_engine.plan._errors import PlanCompileError
        from tests.native._chunked_categorical_support import cat_col, make_config

        cfg = make_config([cat_col(mode="deterministic")])
        check_chunked_compatibility(cfg, table="t", registry=_REG)  # admitted
        bad = make_config([cat_col(mode=None)])
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(bad, table="t", registry=_REG)
        assert exc.value.code == "categorical_nondeterministic_not_chunk_safe"


# ---- e. native admission frozen -------------------------------------------------------------


class TestNativeStillDeclines:
    def test_prepare_categorical_declines_nondeterministic_with_the_same_reason(self) -> None:
        prepared, reason = prepare_categorical(
            "c", deterministic=False, namespace="ns", provider_config={"categories": ["A", "B"]}
        )
        assert prepared is None
        assert reason == "categorical_not_deterministic:c"

    def test_prepare_categorical_still_admits_deterministic(self) -> None:
        prepared, reason = prepare_categorical(
            "c", deterministic=True, namespace="ns", provider_config={"categories": ["A", "B"]}
        )
        assert reason is None and prepared is not None

    def test_physical_operator_assertion_is_unchanged(self) -> None:
        import inspect

        from decoy_engine.execution.physical import _shadow_operators

        src = inspect.getsource(_shadow_operators)
        assert "if not binding.categorical_deterministic:" in src
        assert "categorical node reached run_operator with categorical_deterministic=False" in src


def test_frame_is_whole_frame_only_pandas_handler_is_what_runs() -> None:
    """The seeded oracle is the only path: no other route handles non-deterministic categorical."""
    from decoy_engine.execution._strategies._categorical import CategoricalStrategyHandler

    class _Ctx:
        job_seed = _JOB_SEED
        mask_key = _JOB_SEED
        row_offset = 0

    out, _ = CategoricalStrategyHandler().run(
        pd.DataFrame({"c": ["a", "b", "c", "d"]}),
        "c",
        _cat_seed(),
        _Ctx(),  # type: ignore[arg-type]
    )
    assert set(out["c"]) <= {"A", "B", "C"}
