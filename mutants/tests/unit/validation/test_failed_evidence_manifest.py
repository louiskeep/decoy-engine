"""engine-v2 S10 slice 5: failed-job evidence + the quality_metrics manifest forward."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionResult
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.validation.post import PostValidationRunner, QualitySummary
from decoy_engine.validation.post._scan import ScanContext, ScanOutcome

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_EMPTY_PLAN = SimpleNamespace(seed_envelope=SimpleNamespace(per_table=()))
_EMPTY_PROFILE = SimpleNamespace(tables=())


def _run(execution_result: ExecutionResult, config: dict[str, Any]) -> QualitySummary | None:
    return PostValidationRunner().run(
        plan=_EMPTY_PLAN,  # type: ignore[arg-type]
        execution_result=execution_result,
        sources={"t": pa.table({"a": ["x"]})},
        profile=_EMPTY_PROFILE,  # type: ignore[arg-type]
        registry=_REG,
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
        config=config,
    )


class TestQualityMetricsForward:
    def test_flag_on_writes_quality_summary_block(self) -> None:
        result = ExecutionResult(outputs={"t": pa.table({"a": ["MX"]})}, warnings=())
        summary = _run(result, {"post_validation": True})
        assert summary is not None
        block = result.quality_metrics["quality_summary"]  # the S9 M2 carry-forward, now filled
        assert isinstance(block, dict)
        assert "failed_checks" in block
        assert "timing_per_phase" in block
        assert "post_validation_phase_ms" in block["timing_per_phase"]

    def test_flag_off_leaves_quality_metrics_empty(self) -> None:
        result = ExecutionResult(outputs={"t": pa.table({"a": ["MX"]})}, warnings=())
        assert _run(result, {}) is None
        assert result.quality_metrics == {}  # no forward when the phase is not entered


class TestFailedJobEvidence:
    def test_crashing_scan_fails_job_but_keeps_manifest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_ctx: ScanContext) -> ScanOutcome:
            raise RuntimeError("kaboom")

        monkeypatch.setattr("decoy_engine.validation.post._runner.SCANS", (("boom", boom),))
        result = ExecutionResult(outputs={"t": pa.table({"a": ["x"]})}, warnings=())
        summary = _run(result, {"post_validation": True})
        assert summary is not None
        assert "boom" in summary.failed_checks  # the crash fails the job
        assert any(w.code == "scan_crashed" for w in summary.quality_warnings)
        # The manifest block is STILL written despite the crash (atomic finalize).
        assert list(result.quality_metrics["quality_summary"]["failed_checks"]) == ["boom"]
