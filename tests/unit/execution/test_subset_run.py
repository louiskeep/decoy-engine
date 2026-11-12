"""SUB-RUN E2: a `subset:` block narrows what a run masks (AT-11 to AT-17, AT-24 to AT-31)."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import run_pipeline
from decoy_engine.execution import _pipeline_route_dispatch as dispatch
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.pipeline import _subset_stage as stage_mod
from decoy_engine.subset import (
    SubsetError,
    SubsetRequest,
    plan_subset,
    run_subset,
    select_subset,
    subset_inputs_from_config,
    subset_request_from_config,
)
from tests.unit.execution import _subset_run_support as sup

V = "t"
CANARY_SEED = {"table": "customers", "mode": "keys", "key_columns": ["cid"], "keys": [[sup.CANARY]]}


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sup.VAR, "ab" * 32)


def _run(cfg: dict[str, Any], sources: Any, **kw: Any) -> Any:
    return run_pipeline(cfg, sources, engine_version=V, **kw)


def _fk_closed(out: dict[str, pa.Table]) -> None:
    cust, orders, items = (
        set(out[n].column(c).to_pylist())
        for n, c in (("customers", "cid"), ("orders", "oid"), ("items", "oid"))
    )
    assert set(out["orders"].column("cid").to_pylist()) <= cust
    assert set(items) <= orders


class _RootSpy:
    """Records the scratch root; a plain class so nothing is cleaned up on garbage collection."""

    def __init__(self, real: Any, seen: list[Path], held: list[Any]) -> None:
        self._real = real
        self._seen = seen
        held.append(real)

    def __enter__(self) -> Path:
        root: Path = self._real.__enter__()
        self._seen.append(root)
        return root

    def __exit__(self, *exc: Any) -> Any:
        return self._real.__exit__(*exc)


@contextmanager
def _roots(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Path]]:
    seen: list[Path] = []
    held: list[Any] = []
    real = stage_mod.engine_scratch_root
    monkeypatch.setattr(stage_mod, "engine_scratch_root", lambda p: _RootSpy(real(p), seen, held))
    yield seen


def _rewrite_after_select(monkeypatch: pytest.MonkeyPatch, cfg: dict[str, Any], fmt: str) -> None:
    """Change a kept key on disk between the selection and the staging read."""
    real = stage_mod.select_subset

    def changed(*a: Any, **k: Any) -> Any:
        sel = real(*a, **k)
        path = Path(cfg["sources"]["customers"]["path"])
        data = sup.tables()["customers"]
        row = min(sel.survivors["customers"])
        cids = data.column("cid").to_pylist()
        cids[row] = "ZZ"
        new = pa.table({"cid": cids, "name": data.column("name")})
        if fmt == "parquet":
            pq.write_table(new, path)
        else:
            import pyarrow.csv as pacsv

            pacsv.write_csv(new, path)
        return sel

    monkeypatch.setattr(stage_mod, "select_subset", changed)


def test_at11_closed_rows_metrics_and_identity(tmp_path: Path) -> None:
    cfg, data, _ = sup.job(tmp_path)
    res = _run(cfg, data)
    want = sup.kept(cfg, data)
    assert {n: t.num_rows for n, t in res.outputs.items()} == {n: len(v) for n, v in want.items()}
    _fk_closed(res.outputs)
    block = res.quality_metrics["subset"]
    assert set(block) == {"applied", "fingerprint", "tables", "excluded_edges"}
    assert block["applied"] is True and len(block["fingerprint"]) == 64
    assert block["tables"]["customers"] == {"source_rows": 30, "kept_rows": 5, "whole": False}
    srcs, rels, seeds, policy = subset_inputs_from_config(cfg)
    kw: dict[str, Any] = {"sources": srcs, "relationships": rels, "seeds": seeds, "policy": policy}
    from decoy_engine.plan import job_seed_for_config

    kw.update(job_seed=job_seed_for_config(cfg), engine_version=V)
    assert plan_subset(**kw) is not None
    out = run_subset(output_dir=tmp_path / "standalone", **kw)
    assert out is not None
    for name, rows in want.items():
        assert pq.read_table(tmp_path / "standalone" / f"{name}.parquet").num_rows == len(rows)


def test_at12_subset_equals_full_run_restricted_to_kept_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import decoy_engine.plan as plan_pkg

    plans: list[Any] = []
    real = plan_pkg.compile_plan

    def spy(*a: Any, **k: Any) -> Any:
        plans.append(real(*a, **k))
        return plans[-1]

    monkeypatch.setattr(plan_pkg, "compile_plan", spy)
    cfg, data, _ = sup.job(tmp_path)
    full = _run(cfg, data, subset_request="skip")
    sub = _run(cfg, data)
    assert plans[0].pipeline_config_hash == plans[1].pipeline_config_hash
    assert plans[0].profile_hash != plans[1].profile_hash
    for name, rows in sup.kept(cfg, data).items():
        want = full.outputs[name].take(pa.array(rows, type=pa.int64()))
        assert sub.outputs[name].equals(want), name


def test_at13_profile_has_kept_counts_and_clamped_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import decoy_engine.plan as plan_pkg

    cfg, data, _ = sup.job(tmp_path)
    seen: dict[str, Any] = {}
    real = plan_pkg.compile_plan

    def spy(config: Any, profile: Any, **k: Any) -> Any:
        seen["profile"] = profile
        return real(config, profile, **k)

    monkeypatch.setattr(plan_pkg, "compile_plan", spy)
    _run(cfg, data)
    tabs = {t.name: t for t in seen["profile"].tables}
    assert tabs["customers"].row_count == 5 and tabs["customers"].row_count_exact
    for col in tabs["customers"].columns:
        assert col.row_count == 5 and col.null_count <= 5
        assert col.distinct_count is None or col.distinct_count <= 5
    assert tabs["customers"].columns[0].dtype


def test_at13_whole_table_profile_is_untouched(tmp_path: Path) -> None:
    from decoy_engine.execution.pipeline._subset_stage import SubsetStageScope
    from decoy_engine.profile import profile_source

    cfg, data, _ = sup.job(tmp_path)
    request = subset_request_from_config(cfg)
    assert request is not None
    request = dataclasses.replace(request, relationships=request.relationships[:1])
    sel = select_subset(request, data, config=cfg)
    assert "items" in sel.scope.whole
    profile = profile_source(cfg)
    narrowed = SubsetStageScope.restrict_profile(profile, sel)
    before = {t.name: t for t in profile.tables}
    after = {t.name: t for t in narrowed.tables}
    assert after["items"] == before["items"]
    assert after["customers"].row_count == 5


@pytest.mark.parametrize("fmt", ["parquet", "csv"])
@pytest.mark.parametrize("mode", ["auto", "stream", "out_of_core"])
def test_at14_lazy_inputs_give_the_resident_rows(tmp_path: Path, fmt: str, mode: str) -> None:
    from tests.unit.execution.run_stream import _support as rs

    cfg, data, lazy = sup.job(tmp_path, fmt=fmt)
    want = sup.kept(cfg, data)
    sink, target = rs.real_sink(tmp_path)
    res = _run(cfg, lazy, sink=sink, execution_mode=mode)
    assert not any(isinstance(v, stage_mod.LazySource) for v in res.outputs.values())
    for name, rows in want.items():
        assert pq.read_table(target / f"{name}.parquet").num_rows == len(rows), name


def test_at15_lazy_and_resident_inputs_agree_with_typed_key_and_null(tmp_path: Path) -> None:
    cfg, data, lazy = sup.job(tmp_path)
    resident = _run(cfg, data)
    lazy_out = _run(cfg, lazy)
    for name in data:
        assert lazy_out.outputs[name].equals(resident.outputs[name]), name


@pytest.mark.parametrize("fmt", ["parquet", "csv"])
def test_at17_a_source_changed_between_passes_refuses_and_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    cfg, data, lazy = sup.job(tmp_path, fmt=fmt)
    _rewrite_after_select(monkeypatch, cfg, fmt)
    with _roots(monkeypatch) as roots, pytest.raises(ExecutionError) as info:
        _run(cfg, lazy)
    assert info.value.code == "lazy_source_changed"
    assert roots and not any(r.exists() for r in roots)


def test_at17_selection_and_check_reprs_hold_no_key(tmp_path: Path) -> None:
    cfg, data, _ = sup.job(tmp_path, seed=CANARY_SEED)
    request = subset_request_from_config(cfg)
    assert request is not None
    sel = select_subset(request, data, config=cfg)
    assert sup.CANARY not in repr(sel) + repr(sel.materialization_check)


def test_at24_undeclared_header_refuses_before_the_stage(tmp_path: Path) -> None:
    cfg, _, _ = sup.job(tmp_path, fmt="csv")
    path = Path(cfg["sources"]["customers"]["path"])
    path.write_text(f"{sup.CANARY},x\nC1,n1\n", encoding="utf-8")
    lazy = sup.lazy_sources(cfg)
    with pytest.raises(Exception) as info:
        _run(cfg, lazy)
    assert str(getattr(info.value, "code", "")).endswith("undeclared_column")
    assert sup.CANARY not in str(info.value)


def test_at25_forced_count_mismatch_leaks_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, data, lazy = sup.job(tmp_path, seed=CANARY_SEED)
    real = stage_mod.select_subset

    def off_by_one(*a: Any, **k: Any) -> Any:
        sel = real(*a, **k)
        counts = {n: (s, kp + 1 if n == "customers" else kp) for n, (s, kp) in sel.counts.items()}
        return dataclasses.replace(sel, counts=counts)

    monkeypatch.setattr(stage_mod, "select_subset", off_by_one)
    with pytest.raises(SubsetError) as info:
        _run(cfg, lazy)
    assert info.value.code == "subset_materialize_count_mismatch"
    assert sup.CANARY not in str(info.value) + repr(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def _break_route(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("route failed")

    monkeypatch.setattr(dispatch, "execute_route", boom)


def _small_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stage_mod, "resolve_temp_disk_budget", lambda: 1)


def _io_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*a: Any, **k: Any) -> Any:
        (k["root"] / "s0000.subset.parquet").write_bytes(b"x")
        raise OSError("disk")

    monkeypatch.setattr(stage_mod, "stage_typed_sources", fail)


@pytest.mark.parametrize("case", ["success", "route", "budget", "io"])
def test_at26_no_staged_file_survives_any_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    cfg, _, lazy = sup.job(tmp_path)
    {"success": lambda m: None, "route": _break_route, "budget": _small_budget, "io": _io_failure}[
        case
    ](monkeypatch)
    with _roots(monkeypatch) as roots:
        if case == "success":
            res = _run(cfg, lazy)
            assert not any(isinstance(v, stage_mod.LazySource) for v in res.outputs.values())
        else:
            with pytest.raises(Exception):
                _run(cfg, lazy)
    assert roots and not any(r.exists() for r in roots)


def test_at27_the_budget_is_checked_after_every_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.out_of_core import _budget

    calls: list[int] = []
    files: list[str] = []

    def second_fails(root: Path, *, max_bytes: int) -> int:
        calls.append(1)
        if len(calls) == 2:
            files.extend(sorted(p.name for p in root.iterdir()))
            raise ExecutionError(code="out_of_core_temp_disk_exceeded", message="over budget")
        return 0

    monkeypatch.setattr(_budget, "check_temp_disk_budget", second_fails)
    monkeypatch.setattr(stage_mod, "_BATCH_ROWS", 10)
    monkeypatch.setattr(stage_mod, "resolve_temp_disk_budget", lambda: 10**9)
    cfg, _, lazy = sup.job(tmp_path)
    with _roots(monkeypatch) as roots, pytest.raises(ExecutionError) as info:
        _run(cfg, lazy)
    assert info.value.code == "out_of_core_temp_disk_exceeded" and len(calls) == 2
    assert files == ["s0000.subset.parquet"], "the second check came before the first file ended"
    assert not any(r.exists() for r in roots)


@pytest.mark.parametrize("lazy_input", [False, True])
def test_at28_a_seed_matching_no_row_gives_typed_empty_outputs(
    tmp_path: Path, lazy_input: bool
) -> None:
    seed = {
        "table": "customers",
        "mode": "filter",
        "key_columns": ["cid"],
        "predicates": [{"column": "cid", "op": "eq", "value": "NO-SUCH"}],
    }
    cfg, data, lazy = sup.job(tmp_path, seed=seed)
    full = _run(cfg, data, subset_request="skip")
    res = _run(cfg, lazy if lazy_input else data)
    assert res.quality_metrics["subset"]["tables"]["customers"]["kept_rows"] == 0
    assert res.outputs["customers"].num_rows == 0
    assert res.outputs["customers"].schema.names == full.outputs["customers"].schema.names


def test_at29_a_foreign_key_cycle_keeps_every_reference(tmp_path: Path) -> None:
    from decoy_engine.config import PipelineConfig
    from tests._declared_schema import declare_rest

    def col(n: str, ns: str) -> dict[str, str]:
        return {"name": n, "strategy": "hash", "namespace": ns}

    a = pa.table({"a_id": [f"A{i}" for i in range(20)], "b_id": [f"B{i}" for i in range(20)]})
    b = pa.table(
        {"b_id": [f"B{i}" for i in range(20)], "a_id": [f"A{(i + 1) % 20}" for i in range(20)]}
    )
    for n, t in (("a", a), ("b", b)):
        pq.write_table(t, tmp_path / f"{n}.parquet")
    rels = [
        {
            "parent": {"table": "b", "columns": ["b_id"]},
            "namespace": "nb",
            "children": [{"table": "a", "columns": ["b_id"]}],
            "orphan_policy": "preserve",
        },
        {
            "parent": {"table": "a", "columns": ["a_id"]},
            "namespace": "na",
            "children": [{"table": "b", "columns": ["a_id"]}],
            "orphan_policy": "preserve",
        },
    ]
    raw = {
        "version": 2,
        "global_settings": {"seed": 1, "mask_secret_ref": f"env:{sup.VAR}"},
        "sources": {
            n: {"type": "file", "format": "parquet", "path": str(tmp_path / f"{n}.parquet")}
            for n in "ab"
        },
        "tables": [
            {"name": "a", "columns": [col("a_id", "na"), col("b_id", "nb")]},
            {"name": "b", "columns": [col("b_id", "nb"), col("a_id", "na")]},
        ],
        "relationships": rels,
        "targets": {
            n: {"type": "file", "format": "parquet", "path": str(tmp_path / f"o{n}.parquet")}
            for n in "ab"
        },
        "subset": {
            "seeds": [{"table": "a", "mode": "sample", "key_columns": ["a_id"], "count": 2}]
        },
    }
    cfg = PipelineConfig.model_validate(declare_rest(raw)).model_dump()
    out = _run(cfg, {"a": a, "b": b}).outputs
    assert set(out["a"].column("b_id").to_pylist()) <= set(out["b"].column("b_id").to_pylist())
    assert set(out["b"].column("a_id").to_pylist()) <= set(out["a"].column("a_id").to_pylist())


def test_at30_skip_replace_and_bogus_keyword(tmp_path: Path) -> None:
    cfg, data, _ = sup.job(tmp_path)
    skipped = _run(cfg, data, subset_request="skip")
    assert skipped.quality_metrics["subset"] == {"applied": False}
    assert skipped.outputs["customers"].num_rows == 30
    request = subset_request_from_config(cfg)
    assert request is not None
    seed = dataclasses.replace(request.seeds[0], count=3)
    replaced = _run(
        cfg,
        data,
        subset_request=SubsetRequest(
            request.relationships, (seed,), request.policy, request.excluded_edges
        ),
    )
    assert replaced.outputs["customers"].num_rows == 3
    with pytest.raises(ExecutionError) as info:
        _run(cfg, data, subset_request="bogus")
    assert info.value.code == "invalid_execution_knob"


def test_at31_a_config_without_subset_gains_no_subset_key(tmp_path: Path) -> None:
    cfg, data, _ = sup.job(tmp_path)
    cfg["subset"] = None
    res = _run(cfg, data)
    assert "subset" not in res.quality_metrics
    skip = _run(cfg, data, subset_request="skip")
    assert "subset" not in skip.quality_metrics
