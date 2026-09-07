"""T14/T15 (`docs/plans/2026-09-03-p4-task7-route-seam.md` section 5): the
reorder route inherits `_batch_join`'s disk safety UNCHANGED (section 4.3,
Cam's round-3 decision) -- no new disk mechanism. Task 7 touches neither
`_spill_estimate.py` (the route-entry advisory) nor `_budget.check_temp_disk_
budget` (the table-boundary runtime enforcer), so both fire identically
regardless of which per-table driver `decide_route` picked.

Reuses `test_out_of_core_spill_preflight.py`'s own fixture builders + disk-
patching helper rather than re-deriving them.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline import run_pipeline
from tests.unit.execution.test_out_of_core_spill_preflight import (
    _fk_ooc_config,
    _patch_free_disk,
    _sources,
)

# Between the conservative advisory's predicted footprint for this 20-row
# 2-column job (over-predicts, so it WARNS at this level) and the runtime
# cap's actual write (a few KB) -- tight enough to trip the advisory,
# generous enough that the real write still fits under `check_temp_disk_
# budget`'s 90%-of-free enforcement (calibrated empirically for this fixture).
_TIGHT_BUT_SUFFICIENT_FREE_BYTES = 10_000

# Small enough that even the tiny real write cannot fit -- the runtime
# boundary cap must abort.
_CATASTROPHICALLY_SHORT_FREE_BYTES = 100


class TestT14AdvisoryParity:
    """A tight-but-survivable disk: the advisory WARNS (never rejects) and
    the job still completes -- identically whether the table routed through
    `_batch_join` or the reorder driver."""

    def test_batch_join_route_warns_and_proceeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        _patch_free_disk(monkeypatch, _TIGHT_BUT_SUFFICIENT_FREE_BYTES)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution.out_of_core"):
            result = run_pipeline(
                config, sources, engine_version="t14", execution_mode="out_of_core"
            )
        assert any("out-of-core disk advisory" in r.message for r in caplog.records)
        assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"

    def test_reorder_route_warns_and_proceeds_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        _patch_free_disk(monkeypatch, _TIGHT_BUT_SUFFICIENT_FREE_BYTES)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution.out_of_core"):
            result = run_pipeline(
                config,
                sources,
                engine_version="t14",
                execution_mode="out_of_core",
                out_of_core_reorder_threshold_rows=0,  # force every eligible table to reorder
            )
        # Same route-entry advisory, same WARN-not-reject posture -- no
        # reorder-specific hard cap intervened.
        assert any("out-of-core disk advisory" in r.message for r in caplog.records)
        assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"


class TestT15RuntimeBoundaryParity:
    """A genuinely exhausted disk: the SAME runtime enforcer
    (`check_temp_disk_budget`) aborts cleanly, whether the table routed
    through `_batch_join` or the reorder driver."""

    def test_batch_join_route_runtime_cap_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        _patch_free_disk(monkeypatch, _CATASTROPHICALLY_SHORT_FREE_BYTES)
        with pytest.raises(ExecutionError) as exc_info:
            run_pipeline(config, sources, engine_version="t15", execution_mode="out_of_core")
        assert exc_info.value.code == "out_of_core_temp_disk_exceeded"
        assert not (tmp_path / "parent.out.parquet").exists()
        assert not (tmp_path / "child.out.parquet").exists()

    def test_reorder_route_runtime_cap_aborts_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        _patch_free_disk(monkeypatch, _CATASTROPHICALLY_SHORT_FREE_BYTES)
        with pytest.raises(ExecutionError) as exc_info:
            run_pipeline(
                config,
                sources,
                engine_version="t15",
                execution_mode="out_of_core",
                out_of_core_reorder_threshold_rows=0,
            )
        assert exc_info.value.code == "out_of_core_temp_disk_exceeded"
        assert not (tmp_path / "parent.out.parquet").exists()
        assert not (tmp_path / "child.out.parquet").exists()


__all__: list[str] = []
