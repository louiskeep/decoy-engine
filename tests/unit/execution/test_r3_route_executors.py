"""R3 acceptance test 3: each layer-1 route is a named executor over `(ctx, decision)`.

Plan: docs/plans/2026-10-10-r3-run-context-route-executors.md section 6, test 3.

For each executor there are two kinds of test:

* Dispatch witness: `run_pipeline` reaches the executor, exactly once, with a real
  `PipelineRunContext` and a `RouteDecision` whose route is the one the routing inputs select,
  and reaches no other layer-1 executor. The witness wraps the real executor rather than
  replacing it, so the run still completes.
* Parity: the executor, called directly with the `(ctx, decision)` `run_pipeline` handed it,
  reproduces the baseline captured before extraction (`r3_within_route_baseline.json`). This
  exercises the executor as a function of its context alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution import _pipeline_context as ctxmod
from decoy_engine.execution import _pipeline_route_dispatch as dispatch
from decoy_engine.execution import run_pipeline
from tests.unit.execution import _r3_scenarios as sc

pytestmark = pytest.mark.filterwarnings("ignore")

_BASELINE = json.loads((Path(__file__).parent / "r3_within_route_baseline.json").read_text())

# (scenario, executor attribute on the dispatch module that must run for it)
SEQUENTIAL_CASES = [
    "seq_fk",
    "seq_fk_orphans_remap",
    "seq_fk_orphans_warn",
    "seq_fk_loader",
    "seq_fk_explain",
    "seq_fk_transforms_declined",
]
OUT_OF_CORE_CASES = [
    "ooc_fk_forced",
    "ooc_fk_auto",
    "ooc_fk_orphans_remap",
    "ooc_fk_forced_loader",
    "ooc_fk_budget_bytes",
]
# Scenarios that stream into a sink cannot be re-executed on the same context (the sink has
# already committed), so they get the witness only; their parity is the baseline comparison.
SINK_CASES = [("seq_fk_sink", "sequential"), ("ooc_fk_forced_sink", "out_of_core")]
EXECUTORS = {
    "sequential": "execute_sequential_route",
    "out_of_core": "execute_out_of_core_route",
}


class Witness:
    """Wraps the real layer-1 executors on the dispatch module and records each call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, Any, Any]] = []
        self.real: dict[str, Any] = {}
        for route, attr in EXECUTORS.items():
            real = getattr(dispatch, attr)
            self.real[route] = real

            def wrapped(ctx: Any, decision: Any, *, _r: str = route, _real: Any = real) -> Any:
                self.calls.append((_r, ctx, decision))
                return _real(ctx, decision)

            monkeypatch.setattr(dispatch, attr, wrapped)


@pytest.mark.parametrize("name", SEQUENTIAL_CASES)
def test_sequential_dispatch_witness_and_parity(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    witness = Witness(monkeypatch)
    got = sc.success_snapshot(name, tmp_path)
    assert [c[0] for c in witness.calls] == ["sequential"]
    _, ctx, decision = witness.calls[0]
    assert isinstance(ctx, ctxmod.PipelineRunContext)
    assert isinstance(decision, ctxmod.RouteDecision) and decision.route == "sequential"
    assert got == _BASELINE[name]
    _assert_direct_call_matches_baseline(witness.real["sequential"], ctx, decision, name, tmp_path)


@pytest.mark.parametrize("name", OUT_OF_CORE_CASES)
def test_out_of_core_dispatch_witness_and_parity(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    witness = Witness(monkeypatch)
    got = sc.success_snapshot(name, tmp_path)
    assert [c[0] for c in witness.calls] == ["out_of_core"]
    _, ctx, decision = witness.calls[0]
    assert isinstance(ctx, ctxmod.PipelineRunContext)
    assert isinstance(decision, ctxmod.RouteDecision) and decision.route == "out_of_core"
    assert got == _BASELINE[name]
    _assert_direct_call_matches_baseline(witness.real["out_of_core"], ctx, decision, name, tmp_path)


@pytest.mark.parametrize(("name", "route"), SINK_CASES)
def test_sink_streaming_routes_dispatch_to_their_executor(
    name: str, route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    witness = Witness(monkeypatch)
    got = sc.success_snapshot(name, tmp_path)
    assert [c[0] for c in witness.calls] == [route]
    assert got == _BASELINE[name]


@pytest.mark.parametrize("name", ["ff_legacy_single", "ff_generate_mask", "ff_multi_table"])
def test_full_frame_jobs_reach_no_bounded_executor(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    witness = Witness(monkeypatch)
    got = sc.success_snapshot(name, tmp_path)
    assert witness.calls == []
    assert got == _BASELINE[name]


def _assert_direct_call_matches_baseline(
    executor: Any, ctx: Any, decision: Any, name: str, tmp_path: Path
) -> None:
    again = executor(ctx, decision)
    expected = _BASELINE[name]
    projected = sc._relocate(sc.snapshot(sc.Run(again)), tmp_path)
    for key in ("tables", "warnings", "row_errors", "table_kinds", "execution", "execution_plan"):
        assert projected[key] == expected[key], key
    assert projected["quality_metrics"] == expected["quality_metrics"]


def _spy_route_exec(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    from decoy_engine.execution import _pipeline_route_exec as rx

    seen: dict[str, dict[str, Any]] = {}
    for name in ("run_sequential_route", "run_out_of_core_route"):
        real = getattr(rx, name)

        def spy(*, _n: str = name, _real: Any = real, **kwargs: Any) -> Any:
            seen[_n] = kwargs
            return _real(**kwargs)

        monkeypatch.setattr(rx, name, spy)
    return seen


def test_out_of_core_executor_forwards_every_context_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_route_exec(monkeypatch)
    cfg, tables = sc.fk_job(tmp_path)
    run_pipeline(
        cfg,
        tables,
        engine_version=sc.ENGINE_VERSION,
        execution_mode="out_of_core",
        out_of_core_budget_bytes=512 * 1024 * 1024,
        out_of_core_reorder_threshold_rows=3,
        explain_plan=True,
    )
    kw = seen["run_out_of_core_route"]
    assert kw["budget_bytes"] == 512 * 1024 * 1024
    assert kw["out_of_core_reorder_threshold_rows"] == 3
    assert kw["explain_plan"] is True
    assert kw["route_reason"] == "override_out_of_core"
    assert kw["sources_resident"] is True and kw["source_loader"] is None
    assert kw["sink"] is None and kw["table_kinds"] == {"parent": "mask", "child": "mask"}
    assert kw["unconfigured_column_policy"] in ("warn", "error")
    assert set(kw["sources"]) == {"parent", "child"}


def test_sequential_executor_forwards_every_context_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy_route_exec(monkeypatch)
    cfg, tables = sc.fk_job(tmp_path)
    run_pipeline(
        cfg,
        tables,
        engine_version=sc.ENGINE_VERSION,
        out_of_core_threshold_rows=1_000,
        use_byte_estimate_routing=False,
        fpe_chunk_count=7,
        explain_plan=True,
    )
    kw = seen["run_sequential_route"]
    assert kw["fpe_chunk_count"] == 7
    assert kw["explain_plan"] is True
    assert kw["execution_plan_decision"] is not None
    assert kw["route_reason"] == "pure_mask_fk"
    assert kw["sources_resident"] is True and kw["source_loader"] is None
    assert kw["quarantine_config"] is None
    assert callable(kw["loader"]) and kw["sink"] is None
