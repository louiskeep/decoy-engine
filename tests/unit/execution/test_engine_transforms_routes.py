"""`run_pipeline` owns table transforms: output equals an independent oracle, each
table is transformed exactly once, and routes that cannot apply transforms refuse.

Exact Arrow preservation and the schema guard are in `test_engine_transforms_exact.py`.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from pydantic import ValidationError

import decoy_engine
from decoy_engine import run_pipeline
from decoy_engine.execution import PandasExecutionAdapter, _transforms
from decoy_engine.execution._sequential import run_sequential
from decoy_engine.execution._transforms import TransformError
from decoy_engine.plan._errors import PlanCompileError
from tests.unit.execution._transform_testkit import (
    ENGINE_VERSION,
    base_table,
    cleared,
    fk_config,
    fk_tables,
    reference_run,
    reference_transform,
    single_table_config,
    tables_equal,
    validated,
    write_parquet,
)

_NEEDS_TRANSFORMS = "per_table_transforms_present"

OPS: dict[str, list[dict[str, Any]]] = {
    "filter": [{"op": "filter", "expression": "id > 2"}],
    "sort": [{"op": "sort", "by": ["grp"], "ascending": False}],
    "limit": [{"op": "limit", "n": 4}],
    "dedupe": [{"op": "dedupe", "columns": ["grp"]}],
    "derive": [{"op": "derive", "column": "id2", "expression": "id * 2"}],
    "drop_column": [{"op": "drop_column", "columns": ["extra"]}],
    "chain": [
        {"op": "filter", "expression": "id > 1"},
        {"op": "sort", "by": ["grp", "id"], "ascending": [True, False]},
        {"op": "dedupe", "columns": ["grp"]},
        {"op": "derive", "column": "id2", "expression": "id + 10"},
        {"op": "drop_column", "columns": ["extra"]},
        {"op": "limit", "n": 3},
    ],
}


def _run(cfg: dict[str, Any], sources: Any = None, **kw: Any) -> Any:
    return run_pipeline(cfg, sources=sources, engine_version=ENGINE_VERSION, **kw)


@pytest.fixture
def apply_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts `apply_transforms` calls (one entry per call, value = op count)."""
    calls: list[int] = []
    real = _transforms.apply_transforms

    def spy(df: Any, ops: Any) -> Any:
        calls.append(len(ops))
        return real(df, ops)

    monkeypatch.setattr(_transforms, "apply_transforms", spy)
    return calls


# --------------------------------------------------------------------------
# Route drivers: each returns (actual outputs, expected outputs, transform-bearing
# table count, apply_transforms calls made by the ACTUAL run).
# --------------------------------------------------------------------------


def _route_full_frame(tmp_path, ops, calls):
    tbl = base_table()
    cfg = single_table_config(tmp_path, tbl, transforms=ops)
    actual = _run(cfg, {"t": tbl}).outputs
    seen = len(calls)
    expected = reference_run(tmp_path, cfg, {"t": reference_transform(tbl, ops)}).outputs
    return actual, expected, 1, seen


def _route_generate_mask(tmp_path, ops, calls):
    tbl = base_table()
    cfg = single_table_config(tmp_path, tbl, transforms=ops)
    cfg["tables"].append(
        {
            "name": "gen",
            "row_count": 5,
            "generate_columns": [{"name": "seq", "type": "sequence", "start": 1, "step": 1}],
        }
    )
    cfg["targets"]["gen"] = dict(cfg["targets"]["t"], path=str(tmp_path / "gen_out.parquet"))
    cfg = validated(cfg)
    actual = _run(cfg, {"t": tbl}).outputs
    seen = len(calls)
    expected = reference_run(tmp_path, cfg, {"t": reference_transform(tbl, ops)}).outputs
    return actual, expected, 1, seen


def _route_sequential(tmp_path, ops, calls):
    parent, child = fk_tables()
    p_ops = [
        {"op": "filter", "expression": "n >= 2"},
        {"op": "sort", "by": ["n"], "ascending": False},
    ]
    c_ops = [
        {"op": "filter", "expression": "qty > 3"},
        {"op": "derive", "column": "q2", "expression": "qty * 2"},
    ]
    cfg = fk_config(tmp_path, parent, child, parent_transforms=p_ops, child_transforms=c_ops)
    actual = _run(cfg, {"parent": parent, "child": child}, execution_mode="sequential").outputs
    seen = len(calls)
    exp_sources = {
        "parent": reference_transform(parent, p_ops),
        "child": reference_transform(child, c_ops),
    }
    expected = reference_run(tmp_path, cfg, exp_sources, execution_mode="sequential").outputs
    return actual, expected, 2, seen


def _route_isolated(tmp_path, ops, calls):
    from decoy_engine.execution import run_pipeline_isolated

    tbl = base_table()
    cfg = single_table_config(tmp_path, tbl, transforms=ops)
    # In-process so the spy sees the call; the isolated worker runs the same
    # `run_pipeline` entry (covered separately by test_isolated_child below).
    res = run_pipeline_isolated(cfg, {"t": tbl}, engine_version=ENGINE_VERSION, isolate=False)
    seen = len(calls)
    expected = reference_run(tmp_path, cfg, {"t": reference_transform(tbl, ops)}).outputs
    return res.outputs, expected, 1, seen


_ROUTES = {
    "full_frame": _route_full_frame,
    "generate_mask": _route_generate_mask,
    "sequential": _route_sequential,
    "isolated": _route_isolated,
}


class TestAppliedOnceCorrectOutput:
    """Correctness and exactly-once application."""

    @pytest.mark.parametrize(
        ("route", "case"),
        [(r, c) for r in sorted(_ROUTES) for c in sorted(OPS) if r != "sequential"]
        + [("sequential", "chain")],
    )
    def test_output_equals_independent_oracle_and_applied_once(
        self, tmp_path, apply_calls, route, case
    ):
        actual, expected, n_tables, seen = _ROUTES[route](tmp_path, OPS[case], apply_calls)
        for name in expected:
            tables_equal(actual[name], expected[name])
        assert seen == n_tables, f"expected {n_tables} apply_transforms calls, saw {seen}"

    def test_isolated_child_process_applies_transforms(self, tmp_path):
        """The real subprocess worker goes through `run_pipeline` and gets the
        same result as the oracle."""
        from decoy_engine.execution import run_pipeline_isolated

        ops = OPS["chain"]
        tbl = base_table()
        cfg = single_table_config(tmp_path, tbl, transforms=ops)
        res = run_pipeline_isolated(cfg, {"t": tbl}, engine_version=ENGINE_VERSION)
        assert res.outcome == "completed", res.error
        expected = reference_run(tmp_path, cfg, {"t": reference_transform(tbl, ops)}).outputs["t"]
        assert res.outputs is not None
        tables_equal(res.outputs["t"], expected)


class TestAutoChunkIneligible:
    """Auto-chunk is unavailable for transform-bearing jobs."""

    def _tbl(self, n=60):
        return pa.table(
            {
                "id": pa.array(list(range(n)), pa.int64()),
                "k": pa.array([f"k{i % 7}" if i != 40 else "k5" for i in range(n)]),
                "s": pa.array([f"secret{i}" for i in range(n)]),
            }
        )

    def test_limit_returns_exactly_n_rows(self, tmp_path):
        tbl = self._tbl()
        cfg = single_table_config(tmp_path, tbl, transforms=[{"op": "limit", "n": 7}])
        res = _run(
            cfg,
            {"t": tbl},
            auto_chunk=True,
            auto_chunk_threshold_rows=10,
            chunk_size_rows=16,
            explain_plan=True,
        )
        assert res.outputs["t"].num_rows == 7
        assert res.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert res.quality_metrics["execution_plan"]["mode"] != "chunked"
        assert _NEEDS_TRANSFORMS in res.quality_metrics["execution_plan"]["rejections"]["chunked"]

    def test_dedupe_across_chunk_boundary_removes_duplicate(self, tmp_path):
        tbl = self._tbl()
        cfg = single_table_config(tmp_path, tbl, transforms=[{"op": "dedupe", "columns": ["k"]}])
        res = _run(
            cfg, {"t": tbl}, auto_chunk=True, auto_chunk_threshold_rows=10, chunk_size_rows=16
        )
        assert res.outputs["t"].num_rows == 7
        assert res.outputs["t"].column("id").to_pylist() == list(range(7))


def _poisoned_iterable():
    class _Poison:
        def __iter__(self):
            raise AssertionError("chunk iterable must not be consumed")

    return _Poison()


class TestOutOfCore:
    """Out-of-core declines under auto and refuses when explicit."""

    def _job(self, tmp_path):
        parent, child = fk_tables()
        cfg = fk_config(
            tmp_path,
            parent,
            child,
            parent_transforms=[{"op": "filter", "expression": "n >= 2"}],
        )
        return cfg, {"parent": parent, "child": child}

    def test_auto_declines_with_telemetry_field(self, tmp_path):
        cfg, sources = self._job(tmp_path)
        baseline = _run(
            cleared(cfg),
            sources,
            out_of_core_threshold_rows=1,
            use_byte_estimate_routing=False,
        )
        assert baseline.quality_metrics["execution"]["execution_mode"] == "out_of_core"
        res = _run(cfg, sources, out_of_core_threshold_rows=1, use_byte_estimate_routing=False)
        execution = res.quality_metrics["execution"]
        assert execution["execution_mode"] != "out_of_core"
        assert execution["route_reason"] == "pure_mask_fk"
        assert execution["out_of_core_declined"] == _NEEDS_TRANSFORMS
        assert res.outputs["parent"].num_rows == 10

    def test_byte_estimate_bounded_route_also_declines(self, tmp_path):
        cfg, sources = self._job(tmp_path)
        kw = {"out_of_core_budget_bytes": 64 * 1024 * 1024, "use_probe_routing": False}
        baseline = _run(cleared(cfg), sources, **kw)
        assert baseline.quality_metrics["execution"]["route_reason"] == (
            "byte_estimate_bounded_out_of_core"
        )
        res = _run(cfg, sources, **kw)
        execution = res.quality_metrics["execution"]
        assert execution["execution_mode"] == "sequential"
        assert execution["out_of_core_declined"] == _NEEDS_TRANSFORMS

    def test_explicit_raises_before_any_read(self, tmp_path, monkeypatch):
        cfg, _ = self._job(tmp_path)

        def boom(*a, **k):
            raise AssertionError("profile_source must not run")

        monkeypatch.setattr("decoy_engine.profile.profile_source", boom)

        def loader(name):
            raise AssertionError("source_loader must not run")

        with pytest.raises(PlanCompileError) as exc:
            _run(cfg, {}, execution_mode="out_of_core", source_loader=loader)
        assert exc.value.code == _NEEDS_TRANSFORMS


class TestDirectChunkedEntryPointsReject:
    """Direct chunked entry points reject; the planner falls back instead of raising."""

    def _cfg(self, tmp_path, transforms=True):
        tbl = base_table(40)
        ops = [{"op": "limit", "n": 5}] if transforms else []
        return single_table_config(tmp_path, tbl, transforms=ops), tbl

    def test_run_mask_pipeline_chunked(self, tmp_path):
        from decoy_engine.execution._chunked import run_mask_pipeline_chunked

        cfg, _ = self._cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            run_mask_pipeline_chunked(
                cfg, _poisoned_iterable(), table="t", engine_version=ENGINE_VERSION
            )
        assert exc.value.code == _NEEDS_TRANSFORMS

    def test_native_or_oracle_dispatcher(self, tmp_path):
        from decoy_engine.execution.native._dispatch import run_native_or_oracle_chunked

        cfg, _ = self._cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            run_native_or_oracle_chunked(
                cfg, _poisoned_iterable(), table="t", engine_version=ENGINE_VERSION
            )
        assert exc.value.code == _NEEDS_TRANSFORMS

    def test_run_mask_chunked(self, tmp_path):
        """The public chunked dispatcher rejects at call time, before reading a
        chunk, with the same code as the oracle it shares a preflight with."""
        from decoy_engine.execution import run_mask_chunked

        cfg, _ = self._cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            run_mask_chunked(cfg, _poisoned_iterable(), table="t", engine_version=ENGINE_VERSION)
        assert exc.value.code == _NEEDS_TRANSFORMS
        assert exc.value.path == "tables.t.transforms"
        assert "chunked execution would skip them" in str(exc.value)

    def test_physical_mask_and_native_adapters(self, tmp_path):
        from decoy_engine.execution.physical.drivers._chunked import (
            MaskPipelineChunkedAdapter,
            NativeOrOracleChunkedAdapter,
        )

        cfg, _ = self._cfg(tmp_path)
        for adapter in (MaskPipelineChunkedAdapter(), NativeOrOracleChunkedAdapter()):
            with pytest.raises(PlanCompileError) as exc:
                adapter.run(cfg, _poisoned_iterable(), table="t", engine_version=ENGINE_VERSION)
            assert exc.value.code == _NEEDS_TRANSFORMS

    def test_resident_aggregator_rejects_before_slicing(self, tmp_path):
        from decoy_engine.execution.physical.drivers._chunked import (
            ResidentChunkedAggregatorAdapter,
        )

        cfg, tbl = self._cfg(tmp_path)

        class _SlicerSpy:
            def __init__(self, inner):
                self.inner = inner
                self.slices = 0
                self.num_rows = inner.num_rows
                # Reading the schema is not slicing (the fixed-schema producer captures it up
                # front); the transform rejection must still fire before any `slice` call.
                self.schema = inner.schema

            def slice(self, *a, **k):
                self.slices += 1
                return self.inner.slice(*a, **k)

        spy = _SlicerSpy(tbl)
        with pytest.raises(PlanCompileError) as exc:
            ResidentChunkedAggregatorAdapter().run(
                cfg,
                spy,
                table="t",
                engine_version=ENGINE_VERSION,
                registry=decoy_engine.get_default_registry(),
                adapter=None,
                vault_writer=None,
                chunk_size_rows=8,
            )
        assert exc.value.code == _NEEDS_TRANSFORMS
        assert spy.slices == 0

    def test_without_transforms_all_run_unchanged(self, tmp_path):
        from decoy_engine.execution._chunked import run_mask_pipeline_chunked
        from decoy_engine.execution.physical.drivers._chunked import (
            MaskPipelineChunkedAdapter,
            ResidentChunkedAggregatorAdapter,
        )

        cfg, tbl = self._cfg(tmp_path, transforms=False)
        chunks = [tbl.slice(0, 20), tbl.slice(20)]
        out = list(
            run_mask_pipeline_chunked(cfg, iter(chunks), table="t", engine_version=ENGINE_VERSION)
        )
        assert sum(c.num_rows for c in out) == 40
        out = list(
            MaskPipelineChunkedAdapter().run(
                cfg, iter(chunks), table="t", engine_version=ENGINE_VERSION
            )
        )
        assert sum(c.num_rows for c in out) == 40
        outputs, *_ = ResidentChunkedAggregatorAdapter().run(
            cfg,
            tbl,
            table="t",
            engine_version=ENGINE_VERSION,
            registry=decoy_engine.get_default_registry(),
            adapter=PandasExecutionAdapter(),
            vault_writer=None,
            chunk_size_rows=16,
        )
        assert outputs["t"].num_rows == 40

    def test_dispatcher_without_transforms_unchanged(self, tmp_path):
        from decoy_engine.execution.native._dispatch import run_native_or_oracle_chunked

        cfg, tbl = self._cfg(tmp_path, transforms=False)
        out = list(
            run_native_or_oracle_chunked(
                cfg,
                iter([tbl.slice(0, 20), tbl.slice(20)]),
                table="t",
                engine_version=ENGINE_VERSION,
            )
        )
        assert sum(c.num_rows for c in out) == 40

    def test_planner_falls_back_instead_of_raising(self, tmp_path):
        from decoy_engine.execution._planner import classify_job
        from decoy_engine.execution.physical import compile_physical_plan
        from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs

        cfg, tbl = self._cfg(tmp_path)
        inputs = capture_physical_plan_inputs(
            cfg,
            {"t": tbl},
            engine_version=ENGINE_VERSION,
            auto_chunk=True,
            auto_chunk_threshold_rows=1,
        )
        plan = compile_physical_plan(inputs)
        (table_plan,) = plan.tables
        assert table_plan.driver.name == "FULL_FRAME"
        assert _NEEDS_TRANSFORMS in " ".join(r.reason for r in table_plan.rejected_alternatives)
        decision = classify_job(
            cfg,
            plan=inputs.plan,
            registry=inputs.registry,
            relationship_graph=inputs.graph,
            substrate="pandas",
            source_tables={"t": tbl},
            auto_chunk_threshold_rows=1,
        )
        assert decision.mode != "chunked"
        assert _NEEDS_TRANSFORMS in decision.rejections["chunked"]
        for kw in ({"auto_chunk": True}, {"explain_plan": True}):
            res = _run(cfg, {"t": tbl}, auto_chunk_threshold_rows=1, **kw)
            assert res.outputs["t"].num_rows == 5
            assert res.quality_metrics["execution"]["execution_mode"] == "full_frame"


class TestUnifiedSliceAndIdentity:
    """Unified slice declines; the helper returns the same object when it has nothing to do."""

    def test_unified_slice_declines(self, tmp_path, monkeypatch):
        from decoy_engine.execution import _unified_slice

        def boom(*a, **k):
            raise AssertionError("unified slice must decline a transform-bearing job")

        monkeypatch.setattr(_unified_slice, "_execute_admitted", boom)
        tbl = base_table()
        cfg = single_table_config(tmp_path, tbl, transforms=[{"op": "limit", "n": 3}])
        res = _run(cfg, {"t": tbl}, unified_slice_enabled=True)
        assert res.outputs["t"].num_rows == 3

    def test_helper_returns_identical_object_without_transforms_or_for_generate(self, tmp_path):
        from decoy_engine import apply_table_transforms

        tbl = base_table()
        cfg = single_table_config(tmp_path, tbl)
        assert apply_table_transforms(cfg, "t", tbl) is tbl
        cfg["tables"].append(
            {
                "name": "gen",
                "row_count": 2,
                "generate_columns": [{"name": "seq", "type": "sequence", "start": 1, "step": 1}],
                "transforms": [{"op": "limit", "n": 1}],
            }
        )
        assert apply_table_transforms(cfg, "gen", tbl) is tbl


def _int8_fk(n=20):
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)]),
            "note": pa.array([f"s{i}" for i in range(n)]),
            "small": pa.array([i % 100 for i in range(n)], pa.int8()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(n)]),
            "parent_id": pa.array([f"p{i}" for i in range(n)]),
        }
    )
    return parent, child


_WIDEN = [
    {"op": "derive", "column": "wide", "expression": "small * 1000000000000"},
    {"op": "derive", "column": "frac", "expression": "small / 3"},
]


class TestAdmission:
    """Memory admission for transform-bearing jobs."""

    def _job(self, tmp_path):
        parent, child = _int8_fk()
        cfg = fk_config(tmp_path, parent, child, parent_transforms=_WIDEN)
        return cfg, {"parent": parent, "child": child}

    @pytest.mark.parametrize("skip", ["flag_off", "lazy", "over_budget"])
    def test_probe_skipped_stays_bounded(self, tmp_path, monkeypatch, skip):
        from decoy_engine.execution import _probe

        def boom(*a, **k):
            raise AssertionError("probe must not run")

        monkeypatch.setattr(_probe, "probe_peak_bytes", boom)
        kw: dict[str, Any] = {"out_of_core_budget_bytes": 64 * 1024 * 1024}
        if skip == "over_budget":
            # Raw bytes x the most favorable plausible k exceed the budget; the
            # constant is raised so a small fixture reaches that branch.
            monkeypatch.setattr(_probe, "MIN_PLAUSIBLE_K_FULL_FRAME", 1e9)
        cfg, sources = self._job(tmp_path)
        if skip == "flag_off":
            kw["use_probe_routing"] = False
        if skip == "lazy":
            full = dict(sources)
            sources = {}
            kw["source_loader"] = lambda name: full[name]
        res = _run(cfg, sources, **kw)
        execution = res.quality_metrics["execution"]
        assert execution["execution_mode"] in ("sequential", "out_of_core")
        assert execution["route_reason"] != "byte_estimate_full_frame_fits"

    def test_large_single_table_runs_full_frame_known_limit(self, tmp_path):
        """Characterization of the recorded limit: transforms make auto-chunk
        unavailable and no new rejection exists for a large non-FK job."""
        tbl = base_table(60)
        cfg = single_table_config(tmp_path, tbl, transforms=[{"op": "limit", "n": 5}])
        res = _run(
            cfg,
            {"t": tbl},
            auto_chunk_threshold_rows=1,
            out_of_core_threshold_rows=1,
            full_frame_reject_rows=1,
        )
        assert res.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert res.outputs["t"].num_rows == 5

    def test_33_ops_fail_validation(self, tmp_path):
        ops = [{"op": "limit", "n": 100}] * 33
        tbl = base_table()
        with pytest.raises(ValidationError):
            single_table_config(tmp_path, tbl, transforms=ops)
        ok = [{"op": "limit", "n": 100}] * 32
        single_table_config(tmp_path, tbl, transforms=ok)


class _RecordingSink:
    def __init__(self):
        self.events: list[str] = []

    def write(self, table, data):
        self.events.append(f"write:{table}")

    def write_batches(self, table, batches, *, schema):
        self.events.append(f"write_batches:{table}")

    def commit(self):
        self.events.append("commit")

    def abort(self):
        self.events.append("abort")


class TestInvalidTransforms:
    """Invalid transforms fail loudly and publish nothing."""

    @pytest.mark.parametrize(
        ("ops", "code"),
        [
            (
                [{"op": "derive", "column": "id", "expression": "id + 1"}],
                "derive_column_already_exists",
            ),
            ([{"op": "drop_column", "columns": ["nope"]}], "drop_column_missing"),
            ([{"op": "sort", "by": ["nope"]}], "sort_column_missing"),
        ],
    )
    def test_full_frame_raises_and_writes_nothing(self, tmp_path, ops, code):
        from decoy_engine.vault import VaultWriter

        tbl = base_table()
        cfg = single_table_config(tmp_path, tbl, transforms=ops)
        cfg["quarantine"] = {
            "enabled": True,
            "output_path": str(tmp_path / "quarantine.jsonl"),
            "triggers": ["validation_fail"],
        }
        cfg["validators"] = [{"name": "regex_match", "columns": {"t": ["s"]}, "params": {}}]
        vault = VaultWriter((11).to_bytes(8, "big"))
        before = sorted(p.name for p in tmp_path.iterdir())
        with pytest.raises(TransformError) as exc:
            _run(cfg, {"t": tbl}, vault_writer=vault)
        assert exc.value.code == code
        assert sorted(p.name for p in tmp_path.iterdir()) == before
        assert not vault._entries

    def test_sequential_commits_and_publishes_nothing(self, tmp_path):
        parent, child = fk_tables()
        cfg = fk_config(
            tmp_path,
            parent,
            child,
            parent_transforms=[{"op": "drop_column", "columns": ["nope"]}],
        )
        sink = _RecordingSink()
        with pytest.raises(TransformError) as exc:
            _run(cfg, {"parent": parent, "child": child}, execution_mode="sequential", sink=sink)
        assert exc.value.code == "drop_column_missing"
        assert "commit" not in sink.events
        # Resident tables are transformed before the route starts, so the sink is never touched.
        assert sink.events == []

    def test_resident_tables_are_prepared_before_any_write(self, tmp_path):
        parent, child = fk_tables()
        cfg = fk_config(
            tmp_path,
            parent,
            child,
            child_transforms=[{"op": "sort", "by": ["nope"]}],
        )
        held: list[str] = []
        with pytest.raises(TransformError):
            _run(
                cfg,
                {"parent": parent, "child": child},
                execution_mode="sequential",
                sink=lambda name, data: held.append(name),
            )
        assert held == []


class TestPublicSurface:
    """Public surface."""

    def test_exported(self):
        from decoy_engine import apply_table_transforms

        assert "apply_table_transforms" in decoy_engine.__all__
        assert callable(apply_table_transforms)

    def test_plan_level_docstrings_state_prepared_input_contract(self):
        from decoy_engine.execution.out_of_core import run_fk_out_of_core

        for fn in (PandasExecutionAdapter.run, run_sequential, run_fk_out_of_core):
            assert "must already be transformed" in inspect.getdoc(fn), fn


class TestDerivedColumnMasking:
    """A strategy on a derived column sees the transform frame's dtype."""

    def test_strategy_sees_the_transform_frame_dtype(self, tmp_path, monkeypatch):
        seen: dict[str, Any] = {}
        real = PandasExecutionAdapter._dispatch_mask_node

        def spy(self, node, frames, *a, **k):
            if node.table == "t" and "dcode" in frames[node.table].columns:
                seen[tuple(node.columns)] = frames[node.table]["dcode"].dtype
            return real(self, node, frames, *a, **k)

        monkeypatch.setattr(PandasExecutionAdapter, "_dispatch_mask_node", spy)
        tbl = base_table()
        ops = [{"op": "derive", "column": "dcode", "expression": "qn + 1"}]
        cfg = single_table_config(
            tmp_path,
            tbl,
            transforms=ops,
            columns=[
                {"name": "s", "strategy": "redact"},
                {"name": "dcode", "strategy": "passthrough"},
            ],
        )
        res = _run(cfg, {"t": tbl})
        from decoy_engine.execution._transforms_table import to_transform_frame

        frame = _transforms.apply_transforms(to_transform_frame(tbl), [])
        expected_dtype = _transforms.apply_transforms(frame, _parsed(ops)).dcode.dtype
        assert seen, "derived column never reached the strategy loop"
        assert all(d == expected_dtype for d in seen.values())
        assert "dcode" in res.outputs["t"].column_names


def _parsed(ops):
    from decoy_engine.config import TableConfig

    return TableConfig.model_validate(
        {"name": "x", "columns": [{"name": "a", "strategy": "redact"}], "transforms": ops}
    ).transforms


class TestLazySources:
    def test_isolated_fk_child_with_lazy_sources_applies_transforms(self, tmp_path):
        from decoy_engine.execution import run_pipeline_isolated

        parent, child = fk_tables()
        p_ops = [{"op": "filter", "expression": "n >= 2"}]
        cfg = fk_config(tmp_path, parent, child, parent_transforms=p_ops)
        res = run_pipeline_isolated(
            cfg,
            {"parent": parent, "child": child},
            engine_version=ENGINE_VERSION,
            execution_mode="sequential",
        )
        assert res.outcome == "completed", res.error
        expected = reference_run(
            tmp_path,
            cfg,
            {"parent": reference_transform(parent, p_ops), "child": child},
            execution_mode="sequential",
        ).outputs
        assert res.outputs is not None
        tables_equal(res.outputs["parent"], expected["parent"])
        tables_equal(res.outputs["child"], expected["child"])

    def test_lazy_source_is_schema_checked_before_it_is_read(self, tmp_path, monkeypatch):
        from decoy_engine.profile._readers import LazySource

        dup = pa.Table.from_arrays(
            [pa.array([1, 2]), pa.array([3, 4]), pa.array(["a", "b"])], names=["a", "a", "s"]
        )
        path = write_parquet(tmp_path, dup, "dup")
        lazy = LazySource(path=Path(path))

        def boom(self):
            raise AssertionError("read before the schema guard")

        monkeypatch.setattr(LazySource, "to_table", boom)
        from decoy_engine.execution._pipeline_sources import resolve_resident_sources

        cfg = single_table_config(
            tmp_path, pa.table({"a": [1], "s": ["x"]}), transforms=[{"op": "limit", "n": 1}]
        )
        with pytest.raises(TransformError) as exc:
            resolve_resident_sources({"t": lazy}, config=cfg)
        assert exc.value.code == "duplicate_source_field_names"
