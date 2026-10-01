"""Memory admission for transform-bearing jobs: the transformed tables are priced,
prepared once, and lazy sources never get a full-frame admission under `auto`."""

from __future__ import annotations

import gc
import warnings
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_pipeline
from decoy_engine.errors import ConfigError
from decoy_engine.execution import _transforms, _transforms_table
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.profile._readers import LazySource
from decoy_engine.vault import VaultWriter
from tests.unit.execution._transform_testkit import (
    ENGINE_VERSION,
    cleared,
    fk_config,
    fk_tables,
    hash_col,
    reference_run,
    reference_transform,
    single_table_config,
    tables_equal,
    validated,
    write_parquet,
)

_REJECT = "fk_full_frame_oom_risk_rejected"
_VALIDATORS = [
    {"name": "regex_match", "columns": {"parent": ["note"]}, "params": {"pattern": ".*"}}
]

P_OPS = [
    {"op": "derive", "column": "wide", "expression": "n * 1000"},
    {"op": "filter", "expression": "n >= 2"},
]
C_OPS = [{"op": "filter", "expression": "qty > 3"}]


def _run(cfg: dict[str, Any], sources: Any = None, **kw: Any) -> Any:
    return run_pipeline(cfg, sources=sources, engine_version=ENGINE_VERSION, **kw)


def _execution(res: Any) -> dict[str, Any]:
    return res.quality_metrics["execution"]


@pytest.fixture
def apply_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = _transforms.apply_transforms

    def spy(df: Any, ops: Any) -> Any:
        calls.append(len(ops))
        return real(df, ops)

    monkeypatch.setattr(_transforms, "apply_transforms", spy)
    return calls


@pytest.fixture
def fits_specs(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Records every spec tuple the byte estimator is asked about."""
    from decoy_engine.execution import _mem_estimate

    seen: list[Any] = []
    real = _mem_estimate.fits

    def spy(specs: Any, path: str, budget: int, **kw: Any) -> Any:
        seen.append(specs)
        return real(specs, path, budget, **kw)

    monkeypatch.setattr(_mem_estimate, "fits", spy)
    return seen


def _probe_spy(monkeypatch: pytest.MonkeyPatch, *, conclusive: bool) -> list[dict[str, Any]]:
    from decoy_engine.execution import _probe

    seen: list[dict[str, Any]] = []

    def fake(config: Any, sources: Any, **kw: Any) -> Any:
        seen.append({"config": config, "sources": sources, **kw})
        if conclusive:
            return _probe.ProbeResult(conclusive=True, reason="ok", estimated_peak_bytes=1)
        return _probe.ProbeResult(conclusive=False, reason="inconclusive")

    monkeypatch.setattr(_probe, "probe_peak_bytes", fake)
    return seen


def _job(tmp_path: Path, **ops: Any) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    parent, child = fk_tables()
    cfg = fk_config(
        tmp_path,
        parent,
        child,
        parent_transforms=ops.get("parent", P_OPS),
        child_transforms=ops.get("child", C_OPS),
    )
    return cfg, {"parent": parent, "child": child}


def _reference_tables(sources: dict[str, pa.Table], cfg: dict[str, Any]) -> dict[str, pa.Table]:
    out = {}
    for name, tbl in sources.items():
        entry = next(t for t in cfg["tables"] if t["name"] == name)
        out[name] = reference_transform(tbl, entry["transforms"]) if entry["transforms"] else tbl
    return out


def _cyclic_job(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    n = 8
    a = pa.table(
        {
            "id": pa.array([f"a{i}" for i in range(n)]),
            "ref_b": pa.array([f"b{i}" for i in range(n)]),
            "k": pa.array(list(range(n)), pa.int64()),
        }
    )
    b = pa.table(
        {
            "id": pa.array([f"b{i}" for i in range(n)]),
            "ref_a": pa.array([f"a{i}" for i in range(n)]),
        }
    )
    cfg = validated(
        {
            "version": 1,
            "global_settings": {"seed": 7},
            "sources": {
                "a": {"type": "file", "format": "parquet", "path": write_parquet(tmp_path, a, "a")},
                "b": {"type": "file", "format": "parquet", "path": write_parquet(tmp_path, b, "b")},
            },
            "targets": {
                "a": {"type": "file", "format": "parquet", "path": str(tmp_path / "a_out.parquet")},
                "b": {"type": "file", "format": "parquet", "path": str(tmp_path / "b_out.parquet")},
            },
            "tables": [
                {
                    "name": "a",
                    "columns": [hash_col("id", "na"), hash_col("ref_b", "nb")],
                    "transforms": [
                        {"op": "derive", "column": "k2", "expression": "k * 2"},
                        {"op": "filter", "expression": "k >= 1"},
                    ],
                },
                {"name": "b", "columns": [hash_col("id", "nb"), hash_col("ref_a", "na")]},
            ],
            "relationships": [
                {
                    "parent": {"table": "a", "columns": ["id"]},
                    "children": [{"table": "b", "columns": ["ref_a"]}],
                    "orphan_policy": "preserve",
                    "namespace": "na",
                },
                {
                    "parent": {"table": "b", "columns": ["id"]},
                    "children": [{"table": "a", "columns": ["ref_b"]}],
                    "orphan_policy": "preserve",
                    "namespace": "nb",
                },
            ],
        }
    )
    return cfg, {"a": a, "b": b}


# --------------------------------------------------------------------------
# 12 (i): the transformed table is priced, so small special jobs run full-frame
# --------------------------------------------------------------------------


def _special_kwargs(kind: str) -> dict[str, Any]:
    if kind == "fidelity":
        return {"fidelity_report": True}
    if kind == "post_validation":
        return {"post_validation": True}
    if kind == "vault":
        return {"vault_writer": VaultWriter((7).to_bytes(8, "big"))}
    return {}


class TestResidentJobsArePriced:
    @pytest.mark.parametrize(
        "kind", ["plain", "fidelity", "post_validation", "vault", "validators"]
    )
    def test_small_job_runs_full_frame_and_equals_the_reference(self, tmp_path, kind):
        cfg, sources = _job(tmp_path)
        if kind == "validators":
            cfg["validators"] = _VALIDATORS
        res = _run(cfg, sources, **_special_kwargs(kind))
        assert _execution(res)["execution_mode"] == "full_frame"
        expected = reference_run(
            tmp_path, cfg, _reference_tables(sources, cfg), **_special_kwargs(kind)
        )
        for name in expected.outputs:
            tables_equal(res.outputs[name], expected.outputs[name])

    def test_small_cyclic_job_runs_full_frame(self, tmp_path):
        cfg, sources = _cyclic_job(tmp_path)
        res = _run(cfg, sources)
        assert _execution(res)["execution_mode"] == "full_frame"
        expected = reference_run(tmp_path, cfg, _reference_tables(sources, cfg))
        for name in expected.outputs:
            tables_equal(res.outputs[name], expected.outputs[name])

    def test_estimator_prices_the_prepared_table_not_the_raw_one(self, tmp_path, fits_specs):
        cfg, sources = _job(
            tmp_path,
            parent=[
                {"op": "derive", "column": "wide", "expression": "n * 1000"},
                {"op": "filter", "expression": "n >= 4"},
                {"op": "drop_column", "columns": ["n"]},
            ],
        )
        _run(cfg, sources, out_of_core_budget_bytes=1 << 30)
        parent_spec = next(s for specs in fits_specs for s in specs if s.name == "parent")
        names = [c.name for c in parent_spec.columns]
        assert parent_spec.row_count == 8
        assert "wide" in names
        assert "n" not in names

    def test_routing_equals_the_reference_routing_except_out_of_core(self, tmp_path):
        cfg, sources = _job(tmp_path)
        kw = {"out_of_core_threshold_rows": 1, "use_byte_estimate_routing": False}
        actual = _run(cfg, sources, **kw)
        reference = reference_run(tmp_path, cfg, _reference_tables(sources, cfg), **kw)
        assert _execution(reference)["execution_mode"] == "out_of_core"
        assert _execution(actual)["execution_mode"] == "sequential"
        small = _run(cfg, sources)
        small_reference = reference_run(tmp_path, cfg, _reference_tables(sources, cfg))
        assert _execution(small)["execution_mode"] == _execution(small_reference)["execution_mode"]
        assert _execution(small)["route_reason"] == _execution(small_reference)["route_reason"]

    def test_no_reconciliation_warning_for_a_shrunk_prepared_table(self, tmp_path):
        cfg, sources = _job(tmp_path)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _run(cfg, sources, execution_mode="sequential")
        assert not [w for w in caught if "row count disagrees" in str(w.message)]


# A wide job: the raw table fits a modest budget, the prepared table (many
# derived float columns) does not.
_WIDE_ROWS = 100_000
_WIDE_BUDGET = 400_000_000


def _wide_job(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(_WIDE_ROWS)]),
            "note": pa.array([f"s{i % 97}" for i in range(_WIDE_ROWS)]),
            "n": pa.array(list(range(_WIDE_ROWS)), pa.int64()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(1000)]),
            "parent_id": pa.array([f"p{i}" for i in range(1000)]),
        }
    )
    ops = [{"op": "derive", "column": f"d{k}", "expression": f"n * 1.5 + {k}"} for k in range(20)]
    return fk_config(tmp_path, parent, child, parent_transforms=ops), {
        "parent": parent,
        "child": child,
    }


class TestWideJob:
    def test_raw_fits_but_prepared_is_not_admitted_to_full_frame(self, tmp_path):
        cfg, sources = _wide_job(tmp_path)
        baseline = _run(cleared(cfg), sources, out_of_core_budget_bytes=_WIDE_BUDGET)
        assert _execution(baseline)["route_reason"] == "byte_estimate_full_frame_fits"
        res = _run(cfg, sources, out_of_core_budget_bytes=_WIDE_BUDGET, use_probe_routing=False)
        assert _execution(res)["execution_mode"] == "sequential"
        assert _execution(res)["out_of_core_declined"] == "per_table_transforms_present"
        assert "d19" in res.outputs["parent"].column_names

    def test_prepared_once_across_routing_probe_and_execution(
        self, tmp_path, monkeypatch, apply_calls
    ):
        cfg, sources = _wide_job(tmp_path)
        seen = _probe_spy(monkeypatch, conclusive=True)
        res = _run(
            cfg,
            sources,
            out_of_core_budget_bytes=_WIDE_BUDGET,
            fidelity_report=True,
            post_validation=True,
        )
        assert _execution(res)["route_reason"] == "probe_recovered_full_frame"
        assert apply_calls == [20]
        assert len(seen) == 1
        probe_cfg = seen[0]["config"]
        assert all(not t["transforms"] for t in probe_cfg["tables"])
        assert "d19" in seen[0]["sources"]["parent"].column_names
        assert seen[0]["target_rows"] == _WIDE_ROWS


# --------------------------------------------------------------------------
# 12 (ii)-(iv), 12b (ii), (iii): lazy sources and source authority
# --------------------------------------------------------------------------


def _lazy_sources(cfg: dict[str, Any], kind: str, sources: dict[str, pa.Table]) -> dict[str, Any]:
    if kind == "lazy_source":
        return {n: LazySource(path=Path(cfg["sources"][n]["path"])) for n in sources}
    return {}


def _lazy_kwargs(kind: str, sources: dict[str, pa.Table]) -> dict[str, Any]:
    return {"source_loader": (lambda name: sources[name])} if kind == "loader" else {}


class TestLazySources:
    @pytest.mark.parametrize("kind", ["lazy_source", "loader"])
    @pytest.mark.parametrize("flag", [True, False])
    def test_eligible_job_is_sequential_never_full_frame(self, tmp_path, kind, flag):
        cfg, sources = _job(tmp_path)
        res = _run(
            cfg,
            _lazy_sources(cfg, kind, sources),
            use_byte_estimate_routing=flag,
            **_lazy_kwargs(kind, sources),
        )
        assert _execution(res)["execution_mode"] == "sequential"
        expected = reference_run(
            tmp_path, cfg, _reference_tables(sources, cfg), execution_mode="sequential"
        )
        for name in expected.outputs:
            tables_equal(res.outputs[name], expected.outputs[name])

    @pytest.mark.parametrize("kind", ["lazy_source", "loader"])
    @pytest.mark.parametrize("flag", [True, False])
    @pytest.mark.parametrize("reject_rows", [1, 10**9])
    @pytest.mark.parametrize("shape", ["disqualified", "cyclic"])
    def test_ineligible_job_is_rejected_with_the_base_code(
        self, tmp_path, kind, flag, reject_rows, shape
    ):
        if shape == "cyclic":
            cfg, sources = _cyclic_job(tmp_path)
        else:
            cfg, sources = _job(tmp_path)
            cfg["validators"] = _VALIDATORS
        with pytest.raises(ExecutionError) as exc:
            _run(
                cfg,
                _lazy_sources(cfg, kind, sources),
                use_byte_estimate_routing=flag,
                full_frame_reject_rows=reject_rows,
                **_lazy_kwargs(kind, sources),
            )
        assert exc.value.code == _REJECT

    def test_estimated_row_count_still_gets_the_base_code(self, tmp_path):
        cfg, sources = _job(tmp_path)
        cfg["validators"] = _VALIDATORS
        for name, tbl in sources.items():
            csv_path = tmp_path / f"{name}.csv"
            tbl.to_pandas().to_csv(csv_path, index=False)
            cfg["sources"][name] = {"type": "file", "format": "csv", "path": str(csv_path)}
        with pytest.raises(ExecutionError) as exc:
            _run(
                cfg,
                {},
                use_byte_estimate_routing=False,
                full_frame_reject_rows=1,
                source_loader=lambda name: sources[name],
            )
        assert exc.value.code == _REJECT

    def test_probe_never_runs_for_a_lazy_table(self, tmp_path, monkeypatch):
        cfg, sources = _job(tmp_path)
        seen = _probe_spy(monkeypatch, conclusive=True)
        res = _run(
            cfg, _lazy_sources(cfg, "lazy_source", sources), out_of_core_budget_bytes=1 << 30
        )
        assert seen == []
        assert _execution(res)["execution_mode"] == "sequential"

    @pytest.mark.parametrize("kind", ["lazy_source", "loader"])
    def test_explicit_full_frame_runs(self, tmp_path, kind):
        cfg, sources = _job(tmp_path)
        cfg["validators"] = _VALIDATORS
        res = _run(
            cfg,
            _lazy_sources(cfg, kind, sources),
            execution_mode="full_frame",
            **_lazy_kwargs(kind, sources),
        )
        assert _execution(res)["execution_mode"] == "full_frame"
        expected = reference_run(tmp_path, cfg, _reference_tables(sources, cfg))
        for name in expected.outputs:
            tables_equal(res.outputs[name], expected.outputs[name])

    def test_mixed_residency_transforms_each_table_once(self, tmp_path, apply_calls):
        cfg, sources = _job(
            tmp_path,
            child=[
                {"op": "derive", "column": "q2", "expression": "qty * 2"},
                {"op": "filter", "expression": "qty > 3"},
            ],
        )
        mixed = {
            "parent": sources["parent"],
            "child": LazySource(path=Path(cfg["sources"]["child"]["path"])),
        }
        res = _run(cfg, mixed)
        assert _execution(res)["execution_mode"] == "sequential"
        assert apply_calls == [2, 2]
        assert "q2" in res.outputs["child"].column_names

    def test_loader_only_failure_after_an_earlier_table_is_the_documented_limit(self, tmp_path):
        cfg, sources = _job(tmp_path, child=[{"op": "sort", "by": ["nope"]}])
        held: list[str] = []
        with pytest.raises(_transforms.TransformError):
            _run(
                cfg,
                {},
                execution_mode="sequential",
                source_loader=lambda name: sources[name],
                sink=lambda name, data: held.append(name),
            )
        assert held == ["parent"]

    def test_missing_source_without_a_loader_is_a_coded_error(self, tmp_path):
        cfg, sources = _job(tmp_path)
        with pytest.raises(ExecutionError) as exc:
            _run(cfg, {}, execution_mode="sequential")
        assert exc.value.code == "transform_source_missing"

    @pytest.mark.parametrize("mode", ["auto", "full_frame", "sequential"])
    def test_resident_table_wins_over_the_loader_on_every_route(self, tmp_path, mode, fits_specs):
        cfg, sources = _job(tmp_path)
        other_parent = pa.table(
            {
                "id": pa.array([f"p{i}" for i in range(40)]),
                "note": pa.array([f"zzz{i}" for i in range(40)]),
                "n": pa.array(list(range(100, 140)), pa.int64()),
            }
        )
        loader_tables = {"parent": other_parent, "child": sources["child"]}
        res = _run(
            cfg,
            {"parent": sources["parent"], "child": sources["child"]},
            execution_mode=mode,
            source_loader=lambda name: loader_tables[name],
        )
        expected_parent = reference_transform(sources["parent"], P_OPS)
        assert res.outputs["parent"].num_rows == expected_parent.num_rows == 10
        assert res.outputs["parent"].column("n").to_pylist() == list(range(2, 12))
        if mode == "auto":
            parent_spec = next(s for specs in fits_specs for s in specs if s.name == "parent")
            assert parent_spec.row_count == expected_parent.num_rows


class TestExplicitModes:
    def _poison(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("must not run before the explicit-mode rejection")

        monkeypatch.setattr(_transforms, "apply_transforms", boom)
        monkeypatch.setattr(_transforms, "check_transform_source_schema", boom)
        monkeypatch.setattr(_transforms_table, "to_transform_frame", boom)

    def test_explicit_sequential_rejects_a_disqualified_job_before_any_preparation(
        self, tmp_path, monkeypatch
    ):
        cfg, sources = _job(tmp_path)
        cfg["validators"] = _VALIDATORS
        self._poison(monkeypatch)
        with pytest.raises(ConfigError):
            _run(cfg, sources, execution_mode="sequential")

    def test_explicit_sequential_rejects_a_cycle_before_any_preparation(
        self, tmp_path, monkeypatch
    ):
        cfg, sources = _cyclic_job(tmp_path)
        self._poison(monkeypatch)
        with pytest.raises(ConfigError):
            _run(cfg, sources, execution_mode="sequential")

    def test_explicit_full_frame_bypasses_admission(self, tmp_path):
        cfg, sources = _wide_job(tmp_path)
        res = _run(cfg, sources, execution_mode="full_frame", out_of_core_budget_bytes=1 << 26)
        assert _execution(res)["route_reason"] == "override_full_frame"


# --------------------------------------------------------------------------
# 12 (v): explain and physical-plan capture see what the run sees
# --------------------------------------------------------------------------


class TestExplainAndCaptureAgree:
    def test_fitting_prepared_job(self, tmp_path):
        from decoy_engine.execution.physical import compile_physical_plan
        from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs

        cfg, sources = _job(tmp_path)
        run = _run(cfg, sources, explain_plan=True)
        inputs = capture_physical_plan_inputs(cfg, sources, engine_version=ENGINE_VERSION)
        plan = compile_physical_plan(inputs)
        assert _execution(run)["execution_mode"] == "full_frame"
        assert [t.driver.value for t in plan.tables] == ["full_frame", "full_frame"]
        assert inputs.out_of_core_facts.full_frame_fits_estimate is True

    def test_wide_job_with_the_probe_off(self, tmp_path):
        from decoy_engine.execution.physical import compile_physical_plan
        from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs

        cfg, sources = _wide_job(tmp_path)
        kw = {"out_of_core_budget_bytes": _WIDE_BUDGET, "use_probe_routing": False}
        run = _run(cfg, sources, **kw)
        inputs = capture_physical_plan_inputs(cfg, sources, engine_version=ENGINE_VERSION, **kw)
        plan = compile_physical_plan(inputs)
        assert _execution(run)["execution_mode"] == "sequential"
        assert {t.driver.value for t in plan.tables} == {"sequential"}
        assert inputs.out_of_core_facts.full_frame_fits_estimate is False

    def test_rejected_lazy_job_raises_the_same_code_at_capture(self, tmp_path):
        from decoy_engine.execution.physical import compile_physical_plan
        from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs

        cfg, sources = _job(tmp_path)
        cfg["validators"] = _VALIDATORS
        lazy = _lazy_sources(cfg, "lazy_source", sources)
        with pytest.raises(ExecutionError) as run_exc:
            _run(cfg, lazy)
        inputs = capture_physical_plan_inputs(cfg, lazy, engine_version=ENGINE_VERSION)
        with pytest.raises(ExecutionError) as plan_exc:
            compile_physical_plan(inputs)
        assert run_exc.value.code == plan_exc.value.code == _REJECT


# --------------------------------------------------------------------------
# 12b (iv), (v): probe inputs and the lifetime of the raw table
# --------------------------------------------------------------------------


class TestProbeOnPreparedTables:
    def _shrinking_job(self, tmp_path):
        parent, child = fk_tables(40)
        cfg = fk_config(
            tmp_path,
            parent,
            child,
            parent_transforms=[{"op": "filter", "expression": "n >= 30"}],
            child_transforms=[{"op": "filter", "expression": "qty > 70"}],
        )
        return cfg, {"parent": parent, "child": child}

    def test_probe_uses_prepared_row_counts(self, tmp_path, monkeypatch):
        cfg, sources = self._shrinking_job(tmp_path)
        seen = _probe_spy(monkeypatch, conclusive=True)
        monkeypatch.setattr(
            "decoy_engine.execution._mem_estimate.fits", lambda *a, **k: False, raising=True
        )
        res = _run(cfg, sources, out_of_core_budget_bytes=1 << 30)
        assert len(seen) == 1
        prepared = _reference_tables(sources, cfg)
        assert seen[0]["target_rows"] == max(t.num_rows for t in prepared.values())
        assert seen[0]["reference_table"] == "parent"
        assert {n: t.num_rows for n, t in seen[0]["sources"].items()} == {
            n: t.num_rows for n, t in prepared.items()
        }
        assert _execution(res)["route_reason"] == "probe_recovered_full_frame"

    def test_probe_is_inconclusive_when_distinct_counts_cannot_be_measured(
        self, tmp_path, monkeypatch
    ):
        import pyarrow.compute as pc

        cfg, sources = self._shrinking_job(tmp_path)
        seen = _probe_spy(monkeypatch, conclusive=True)
        monkeypatch.setattr(
            "decoy_engine.execution._mem_estimate.fits", lambda *a, **k: False, raising=True
        )

        def boom(*a, **k):
            raise pa.ArrowNotImplementedError("unsupported")

        monkeypatch.setattr(pc, "count_distinct", boom)
        res = _run(cfg, sources, out_of_core_budget_bytes=1 << 30)
        assert seen == []
        assert _execution(res)["execution_mode"] == "sequential"


class TestPreparedLifetime:
    def test_raw_table_is_not_held_after_preparation(self, tmp_path, monkeypatch):
        from decoy_engine.execution import _pipeline_sources

        parent, child = fk_tables()
        cfg = fk_config(tmp_path, parent, child, parent_transforms=P_OPS)
        sources = {"parent": parent, "child": child}
        holders: list[int] = []
        real = _pipeline_sources.resolve_resident_sources

        def spy(caller_sources, **kw):
            others = [
                r
                for r in gc.get_referrers(parent)
                if isinstance(r, dict) and r is not sources and r.get("parent") is parent
            ]
            holders.append(len(others))
            return real(caller_sources, **kw)

        monkeypatch.setattr(_pipeline_sources, "resolve_resident_sources", spy)
        _run(cfg, sources, execution_mode="full_frame")
        assert holders == [0]


# --------------------------------------------------------------------------
# 14: compile checks read the raw source T (characterization)
# --------------------------------------------------------------------------


class TestCompileChecksOnRawSource:
    def test_filter_that_removes_every_null_is_still_refused(self, tmp_path):
        import pandas as pd

        from decoy_engine.plan._errors import PlanCompileError

        df = pd.DataFrame({"x": pd.array([1, None, 3, 4], dtype="Int8"), "s": ["a", "b", "c", "d"]})
        tbl = pa.Table.from_pandas(df, preserve_index=False)
        cfg = single_table_config(
            tmp_path,
            tbl,
            transforms=[{"op": "filter", "expression": "x > 0"}],
            columns=[{"name": "x", "strategy": "hash", "namespace": "ns"}],
        )
        with pytest.raises(PlanCompileError) as exc:
            _run(cfg, {"t": tbl})
        assert exc.value.code == "null_bearing_int_unsupported"
