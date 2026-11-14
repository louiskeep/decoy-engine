"""AT-26: a post-scan block in an isolated child is its own outcome, not a crash."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.execution import IsolatedRunResult, _governor, run_pipeline_isolated
from decoy_engine.execution._governor import GovernorResult, run_job_with_governor
from decoy_engine.execution._isolated_common import ISOLATED_OUTCOMES
from decoy_engine.execution.pipeline._post_scan import PostScanGate
from tests.unit.execution import _subset_run_support as subset_sup
from tests.unit.execution.post_scan._orphan_defect import install
from tests.unit.execution.test_lazy_path_route_admission import (
    _ooc_eligible_config,
    _parent_child_tables,
)

V = "ps9-isolated-blocked"
ORPHANS = "post_scan.foreign_key_orphans_introduced"
_MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _subset_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(subset_sup.VAR, "ab" * 32)


def _sources() -> dict[str, Any]:
    parent, child = _parent_child_tables()
    return {"parent": parent, "child": child}


def _orphan_config(tmp_path: Path) -> dict[str, Any]:
    config = copy.deepcopy(_ooc_eligible_config(tmp_path, "parquet"))
    config["global_settings"]["mask_secret_ref"] = f"env:{subset_sup.VAR}"
    return config


_ORPHAN_WORKER = """
import sys

from decoy_engine.execution._isolated_worker import main
from tests.unit.execution.post_scan._orphan_defect import install

install()
raise SystemExit(main(sys.argv))
"""


@pytest.fixture
def orphaning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Plant the defect in this process and in a child started through a patched worker."""
    monkeypatch.setattr(PostScanGate, "run_resident", PostScanGate.run_resident)
    install()
    fake_dir = tmp_path / "_fake_workers"
    fake_dir.mkdir()
    (fake_dir / "ps9_orphan_worker.py").write_text(_ORPHAN_WORKER, encoding="utf-8")
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_dir) + (os.pathsep + existing if existing else ""))
    monkeypatch.setattr("decoy_engine.execution._isolated_run._WORKER_MODULE", "ps9_orphan_worker")


def _run(tmp_path: Path, *, isolate: bool) -> tuple[IsolatedRunResult, Path]:
    out = tmp_path / "committed"
    result = run_pipeline_isolated(
        _orphan_config(tmp_path),
        _sources(),
        isolate=isolate,
        output_dir=out,
        engine_version=V,
        execution_mode="full_frame",
    )
    return result, out


@pytest.mark.parametrize("isolate", [True, False], ids=["subprocess", "in_process"])
def test_at26_an_introduced_orphan_is_a_blocked_outcome_with_the_report(
    tmp_path: Path, isolate: bool, orphaning: None
) -> None:
    result, out = _run(tmp_path, isolate=isolate)
    assert result.isolated is isolate
    assert result.outcome == "blocked", (result.error, result.reason)
    assert result.reason is not None
    assert f"{result.reason.namespace}.{result.reason.code}" == ORPHANS
    assert result.outputs is None
    assert result.committed_output_dir is None
    assert not out.exists()
    assert not list(tmp_path.glob("*.out.parquet"))
    report = result.quality_metrics["post_scan_integrity"]
    assert report["verdict"]["blocking"] is True
    codes = {(c["namespace"], c["code"]) for c in report["verdict"]["codes"]}
    assert ("post_scan", "foreign_key_orphans_introduced") in codes


def test_at26_the_blocked_text_carries_no_data_value(tmp_path: Path, orphaning: None) -> None:
    result, _ = _run(tmp_path, isolate=False)
    assert result.error is not None
    assert "REDACTED" not in result.error and "p0" not in result.error


def test_at26_the_governor_returns_a_terminal_blocked_without_rerouting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orphaning: None
) -> None:
    calls: list[str] = []
    real = _governor.run_pipeline_isolated

    def spy(config: Any, sources: Any, *, execution_mode: str, **kw: Any) -> IsolatedRunResult:
        calls.append(execution_mode)
        return real(config, sources, execution_mode=execution_mode, **kw)

    monkeypatch.setattr(_governor, "run_pipeline_isolated", spy)
    for governed in (True, False):
        calls.clear()
        result = run_job_with_governor(
            _orphan_config(tmp_path),
            _sources(),
            budget_bytes=2048 * _MB,
            use_runtime_governor=governed,
            poll_interval_s=0.05,
            engine_version=V,
        )
        assert isinstance(result, GovernorResult)
        assert result.outcome == "blocked"
        assert len(calls) == 1
        assert result.final_route == calls[0]
        assert result.trips == ()
        assert result.result is not None and result.result.outcome == "blocked"
        assert "post_scan_integrity" in result.result.quality_metrics


def test_at26_the_known_outcome_tokens_are_a_closed_set() -> None:
    assert set(ISOLATED_OUTCOMES) == {"completed", "oom_killed", "crashed", "blocked"}


_FAKE_WORKER = """
import json
import sys
from pathlib import Path

envelope = {{"outcome": {outcome}, "peak_rss_mb": 1.0, "error": "x",
            "reason": {{"namespace": "post_scan", "code": "foreign_key_orphans_introduced"}},
            "quality_metrics": {metrics}}}
(Path(sys.argv[1]).parent / "result.json").write_text(json.dumps(envelope))
"""


@pytest.mark.parametrize("outcome", ["true", '"unblocked"', "1", '"BLOCKED"', "null"])
def test_at26_a_bool_or_unknown_outcome_token_is_a_crash_not_a_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    fake_dir = tmp_path / "_fake_workers"
    fake_dir.mkdir()
    (fake_dir / "ps9_fake_worker.py").write_text(
        _FAKE_WORKER.format(outcome=outcome, metrics="{}"), encoding="utf-8"
    )
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_dir) + (os.pathsep + existing if existing else ""))
    monkeypatch.setattr("decoy_engine.execution._isolated_run._WORKER_MODULE", "ps9_fake_worker")
    result = run_pipeline_isolated(
        _orphan_config(tmp_path),
        _sources(),
        isolate=True,
        output_dir=tmp_path / "o",
        engine_version=V,
    )
    assert result.outcome == "crashed"
    assert result.outputs is None
    assert not (tmp_path / "o").exists()


def test_at26_a_blocked_envelope_with_a_wrong_typed_report_keeps_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dir = tmp_path / "_fake_workers"
    fake_dir.mkdir()
    (fake_dir / "ps9_fake_worker.py").write_text(
        _FAKE_WORKER.format(outcome='"blocked"', metrics='"not a mapping"'), encoding="utf-8"
    )
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_dir) + (os.pathsep + existing if existing else ""))
    monkeypatch.setattr("decoy_engine.execution._isolated_run._WORKER_MODULE", "ps9_fake_worker")
    result = run_pipeline_isolated(
        _orphan_config(tmp_path),
        _sources(),
        isolate=True,
        output_dir=tmp_path / "o",
        engine_version=V,
    )
    assert result.outcome == "blocked"
    assert result.quality_metrics == {}
    assert not (tmp_path / "o").exists()
