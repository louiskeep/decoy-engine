"""Task 4.6 activation prerequisite: the widened `_execute_admitted` fail-
closed boundary (FOLLOWUP-UNIFIED-EXCEPTION-BOUNDARY). An admitted job that
hits an unexpected exception anywhere in the physical-plan / shadow-
coordinator / finalize chain must reroute to the legacy route rather than
leak a raw compiler/kernel exception -- see `_unified_slice.py`'s
`_execute_admitted` docstring and the module-level `_REROUTE_LOG` code.

Reuses the mixed passthrough/redact/truncate fixture shape from
`test_unified_slice_parity.py` (`_run_both`'s own sibling cases) so every
stage below activates the same admitted, three-column table -- no hash
column, so no dependency on the optional compiled native companion.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import run_pipeline
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY, UnifiedSliceInvariantError
from tests.physical._shadow_helpers import build_config, write_read_only_fixture
from tests.physical.test_unified_slice_admission import _build, _cheap_ok, _profile_and_plan
from tests.physical.test_unified_slice_parity import _assert_outputs_cell_identical, _key_provider

ENGINE_VERSION = "unified-slice-exception-boundary-test"
_LOGGER_NAME = "decoy_engine.execution._unified_slice"
_STABLE_CODE = "unified_slice_unexpected_exception_reroute"


def _mixed_admitted_fixture(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    """The same passthrough+redact+truncate shape
    `test_redact_and_truncate_mixed_admits_and_matches` admits: three
    columns so the source-shaped assembly loop actually runs (passthrough
    alone would skip it), no hash column so no companion dependency."""
    source = pa.table(
        {
            "p": pa.array(["x", "y", "z"], type=pa.string()),
            "r": pa.array(["s1", "s2", "s3"], type=pa.string()),
            "tr": pa.array(["abcdef", "ghijkl", "mnopqr"], type=pa.string()),
        }
    )
    columns: list[dict[str, Any]] = [
        {"name": "p", "strategy": "passthrough"},
        {"name": "r", "strategy": "redact"},
        {"name": "tr", "strategy": "truncate", "provider_config": {"length": 3}},
    ]
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(tmp_path, "t", path, columns)
    return config, path


def _run(config: dict[str, Any], path: Path, *, flag: bool) -> Any:
    return run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=flag,
    )


def _raise_on_nth_call(monkeypatch: pytest.MonkeyPatch, module: Any, attr: str, *, n: int) -> None:
    """Patch `module.attr` so its Nth call (1-indexed) raises a plain
    `RuntimeError`; every other call delegates to the real implementation.

    Several wrapped stages (`select_execution_adapter`, `stamp_execution_
    metrics`, `finalize_validators_and_quarantine`, `execution_telemetry`)
    are ALSO called by the legacy route this test expects the reroute to
    complete through -- a permanent patch would poison that very legacy
    run, failing the "reroute completes" assertion for the wrong reason.
    `n` picks out which call in the sequence is the unified lane's own.
    """
    original = getattr(module, attr)
    calls = {"count": 0}

    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        if calls["count"] == n:
            raise RuntimeError(f"injected fault: {attr}")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, attr, _wrapper)


# ---------------------------------------------------------------------------
# One setup function per wrapped stage (Codex re-gate: exhaustive). Each
# installs a fault that fires exactly once, at the point the plan names.
# ---------------------------------------------------------------------------


def _stage_select_execution_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution import _substrate

    # call 1 is _pipeline.py's own upfront resolution (must succeed so the
    # legacy reroute has a working adapter); call 2 is _execute_admitted's.
    _raise_on_nth_call(monkeypatch, _substrate, "select_execution_adapter", n=2)


def _stage_build_live_physical_plan_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution.physical import _live_inputs

    _raise_on_nth_call(monkeypatch, _live_inputs, "build_live_physical_plan_inputs", n=1)


def _stage_compile_physical_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution.physical import _compiler

    _raise_on_nth_call(monkeypatch, _compiler, "compile_physical_plan", n=1)


def _stage_resident_contract_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution import _unified_slice_admission

    _raise_on_nth_call(monkeypatch, _unified_slice_admission, "resident_contract_admission", n=1)


def _stage_build_unified_slice_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution.physical import _activation

    _raise_on_nth_call(monkeypatch, _activation, "build_unified_slice_activation", n=1)


def _stage_shadow_context_from_key_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution.physical._shadow_context import ShadowContext

    _raise_on_nth_call(monkeypatch, ShadowContext, "from_key_provider", n=1)


def _stage_capture_shadow_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution.physical import _shadow_snapshot

    _raise_on_nth_call(monkeypatch, _shadow_snapshot, "capture_shadow_snapshot", n=1)


def _stage_shadow_coordinator_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator

    def _wrapper(self: ShadowCoordinator, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("injected fault: ShadowCoordinator.run")

    monkeypatch.setattr(ShadowCoordinator, "run", _wrapper)


def _stage_source_shaped_assembly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poisons only the ONE `CheapCandidate.source_frame` this admitted call
    builds (a per-instance `DataFrame` subclass, not a class-wide pandas
    patch) so the legacy reroute's own, unrelated `df[col] = ...`
    assignments are untouched."""
    from decoy_engine.execution import _unified_slice_admission

    class _RaisingFrame(pd.DataFrame):
        def __setitem__(self, key: Any, value: Any) -> None:
            raise RuntimeError("injected fault: source-shaped assembly")

    original = _unified_slice_admission.cheap_admission

    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        candidate = original(*args, **kwargs)
        if candidate is None:
            return None
        return dataclasses.replace(candidate, source_frame=_RaisingFrame(candidate.source_frame))

    monkeypatch.setattr(_unified_slice_admission, "cheap_admission", _wrapper)


def _stage_stamp_execution_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution import _pipeline_finalize

    # call 1 is the unified lane's own; call 2 is the legacy reroute's.
    _raise_on_nth_call(monkeypatch, _pipeline_finalize, "stamp_execution_metrics", n=1)


def _stage_finalize_validators_and_quarantine(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution import _pipeline_finalize

    _raise_on_nth_call(monkeypatch, _pipeline_finalize, "finalize_validators_and_quarantine", n=1)


def _stage_execution_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution import _pipeline_route_exec

    _raise_on_nth_call(monkeypatch, _pipeline_route_exec, "execution_telemetry", n=1)


def _stage_typed_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution import _unified_slice

    _raise_on_nth_call(monkeypatch, _unified_slice, "_typed_warnings", n=1)


def _stage_execution_result_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution import _adapter

    _raise_on_nth_call(monkeypatch, _adapter, "ExecutionResult", n=1)


_STAGES: list[tuple[str, Any]] = [
    ("select_execution_adapter", _stage_select_execution_adapter),
    ("build_live_physical_plan_inputs", _stage_build_live_physical_plan_inputs),
    ("compile_physical_plan", _stage_compile_physical_plan),
    ("resident_contract_admission", _stage_resident_contract_admission),
    ("build_unified_slice_activation", _stage_build_unified_slice_activation),
    ("shadow_context_from_key_provider", _stage_shadow_context_from_key_provider),
    ("capture_shadow_snapshot", _stage_capture_shadow_snapshot),
    ("shadow_coordinator_run", _stage_shadow_coordinator_run),
    ("source_shaped_assembly", _stage_source_shaped_assembly),
    ("stamp_execution_metrics", _stage_stamp_execution_metrics),
    ("finalize_validators_and_quarantine", _stage_finalize_validators_and_quarantine),
    ("execution_telemetry", _stage_execution_telemetry),
    ("typed_warnings", _stage_typed_warnings),
    ("execution_result_construction", _stage_execution_result_construction),
]


@pytest.mark.parametrize("stage_id,setup", _STAGES, ids=[s[0] for s in _STAGES])
def test_unexpected_exception_reroutes_to_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stage_id: str,
    setup: Any,
) -> None:
    config, path = _mixed_admitted_fixture(tmp_path)
    off = _run(config, path, flag=False)

    setup(monkeypatch)
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    on = _run(config, path, flag=True)

    # Proof the lane declined rather than activated: no D7 leaf, and the
    # reroute produced the SAME outputs and quality_metrics a flag-off run
    # would -- the reroute IS the legacy route, not a second attempt at it.
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    _assert_outputs_cell_identical(off, on)
    assert tuple(off.warnings) == tuple(on.warnings)
    assert off.table_kinds == on.table_kinds
    assert tuple(off.row_errors) == tuple(on.row_errors)
    assert on.quality_metrics == off.quality_metrics

    reroute_records = [r for r in caplog.records if _STABLE_CODE in r.getMessage()]
    assert reroute_records, f"{stage_id}: expected the stable reroute code to fire"
    for record in reroute_records:
        assert "injected fault" not in record.getMessage(), (
            f"{stage_id}: the injected exception's own message must never be logged"
        )
        assert record.exc_info is None, f"{stage_id}: no traceback should be attached"
        assert record.exc_text is None, f"{stage_id}: no traceback should be attached"


# ---------------------------------------------------------------------------
# Every catch clause the widened boundary must NOT reroute: the coordinator's
# own coded divergence, the lane's own invariant guards, and process-control
# signals.
# ---------------------------------------------------------------------------


def test_shadow_difference_still_raises_invariant_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
    from decoy_engine.execution.physical._shadow_diff_codes import CELL_VALUE_DIFF, ShadowDifference

    config, path = _mixed_admitted_fixture(tmp_path)

    def _diff_run(self: ShadowCoordinator, *args: Any, **kwargs: Any) -> Any:
        raise ShadowDifference(
            code=CELL_VALUE_DIFF, detail="forced for the exception-boundary test"
        )

    monkeypatch.setattr(ShadowCoordinator, "run", _diff_run)

    with pytest.raises(UnifiedSliceInvariantError, match="coded shadow difference"):
        _run(config, path, flag=True)


def test_deliberate_invariant_error_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces the D7 evidence-mismatch guard (empty `route_evidence` after a
    real, successful coordinator run) rather than a `ShadowDifference` --
    the lane's OWN internal-consistency check, not a coded coordinator
    divergence -- and confirms the new outer `except Exception` does not
    swallow it either."""
    from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator

    config, path = _mixed_admitted_fixture(tmp_path)
    original_run = ShadowCoordinator.run

    def _strip_evidence(self: ShadowCoordinator, *args: Any, **kwargs: Any) -> Any:
        result = original_run(self, *args, **kwargs)
        return dataclasses.replace(result, route_evidence={})

    monkeypatch.setattr(ShadowCoordinator, "run", _strip_evidence)

    with pytest.raises(UnifiedSliceInvariantError, match="without matching completed-execution"):
        _run(config, path, flag=True)


@pytest.mark.parametrize("exc_cls", [KeyboardInterrupt, SystemExit])
def test_base_exceptions_propagate_uncaught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc_cls: type[BaseException]
) -> None:
    from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator

    config, path = _mixed_admitted_fixture(tmp_path)

    def _raise(self: ShadowCoordinator, *args: Any, **kwargs: Any) -> Any:
        raise exc_cls()

    monkeypatch.setattr(ShadowCoordinator, "run", _raise)

    with pytest.raises(exc_cls):
        _run(config, path, flag=True)


# ---------------------------------------------------------------------------
# Failure-mode 1's conditional proof (plan): cheap admission declines any
# truthy validators/quarantine config, so finalize's quarantine-write branch
# is unreachable on the admitted path -- pinned here, not just asserted in
# the plan prose.
# ---------------------------------------------------------------------------


def test_admission_declines_validators_so_finalize_write_branch_is_unreachable(
    tmp_path: Path,
) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    config = dict(config)
    config["validators"] = [{"name": "x"}]
    assert _cheap_ok(config, profile, source) is None


def test_admission_declines_quarantine_so_finalize_write_branch_is_unreachable(
    tmp_path: Path,
) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    config = dict(config)
    config["quarantine"] = {"enabled": True, "triggers": ["validation_fail"]}
    assert _cheap_ok(config, profile, source) is None
