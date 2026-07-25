"""SP-05 validator framework integration test.

Verifies that:
  - A job with a ``validators:`` block runs the configured validators after
    all column passes complete.
  - The ValidationReport is persisted to the evidence manifest under
    ``quality_metrics["validation"]["validators"]``.
  - A job with a failing validator raises ValidatorFailedError (fail-closed).

Uses ``run_pipeline`` (the V2 unified entry point) rather than the adapter
directly, to exercise the full wiring path. Sources are written to tmp_path
so profile_source can load them from disk (the engine's filesystem boundary).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import ValidatorFailedError
from decoy_engine.execution._pipeline import run_pipeline

_ENGINE_VERSION = "0.1.0"


def _write_source(tmp_path: Path, table: pa.Table, name: str = "t") -> str:
    """Write a pa.Table to a temp parquet file; return the path string."""
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _config(
    src_path: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Minimal valid pipeline config pointing at a file source."""
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "sp05-test", "seed": 42},
        "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
        "targets": {"t": {"type": "file", "path": src_path + ".out", "format": "parquet"}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "cc", "strategy": "passthrough"},
                ],
            }
        ],
        "relationships": [],
    }
    if extra:
        cfg.update(extra)
    return cfg


def _sources(src_path: str) -> dict[str, pa.Table]:
    """Load the Arrow table back from the parquet file for the adapter."""
    return {"t": pq.read_table(src_path)}


class TestValidatorsPassThrough:
    """When all validators pass, the job completes and the report is in quality_metrics."""

    def test_validation_report_in_quality_metrics(self, tmp_path: Path) -> None:
        valid_cc = "4532015112830366"
        src = pa.table({"cc": [valid_cc]})
        src_path = _write_source(tmp_path, src)
        config = _config(
            src_path,
            {"validators": [{"name": "luhn", "columns": {"t": ["cc"]}}]},
        )
        result = run_pipeline(config, _sources(src_path), engine_version=_ENGINE_VERSION)
        assert "validation" in result.quality_metrics
        vblock = result.quality_metrics["validation"]
        assert "validators" in vblock
        report = vblock["validators"]
        assert report["passed"] is True
        assert "luhn" in report["validators_run"]

    def test_no_validators_block_no_report(self, tmp_path: Path) -> None:
        """Without a validators: block, quality_metrics has no validation key."""
        src = pa.table({"cc": ["4532015112830366"]})
        src_path = _write_source(tmp_path, src)
        config = _config(src_path)
        result = run_pipeline(config, _sources(src_path), engine_version=_ENGINE_VERSION)
        assert "validation" not in result.quality_metrics

    def test_elapsed_ms_in_report(self, tmp_path: Path) -> None:
        src = pa.table({"cc": ["4532015112830366"]})
        src_path = _write_source(tmp_path, src)
        config = _config(
            src_path,
            {"validators": [{"name": "luhn", "columns": {"t": ["cc"]}}]},
        )
        result = run_pipeline(config, _sources(src_path), engine_version=_ENGINE_VERSION)
        report = result.quality_metrics["validation"]["validators"]
        assert report["elapsed_ms"] >= 0.0


class TestValidatorsFailClosed:
    """A failing validator raises ValidatorFailedError (fail-closed default)."""

    def test_bad_luhn_fails_job(self, tmp_path: Path) -> None:
        bad_cc = "4532015112830367"  # wrong Luhn check digit
        src = pa.table({"cc": [bad_cc]})
        src_path = _write_source(tmp_path, src)
        config = _config(
            src_path,
            {"validators": [{"name": "luhn", "columns": {"t": ["cc"]}}]},
        )
        with pytest.raises(ValidatorFailedError) as exc_info:
            run_pipeline(config, _sources(src_path), engine_version=_ENGINE_VERSION)
        assert exc_info.value.report.passed is False
        assert len(exc_info.value.report.findings) >= 1

    def test_valid_data_does_not_raise(self, tmp_path: Path) -> None:
        src = pa.table({"cc": ["4111111111111111"]})
        src_path = _write_source(tmp_path, src)
        config = _config(
            src_path,
            {"validators": [{"name": "luhn", "columns": {"t": ["cc"]}}]},
        )
        # Must not raise
        run_pipeline(config, _sources(src_path), engine_version=_ENGINE_VERSION)

    def test_validators_report_in_quality_metrics_on_fail(self, tmp_path: Path) -> None:
        """Even when the job fails, the ValidationReport is in the exception."""
        bad_cc = "4532015112830367"
        src = pa.table({"cc": [bad_cc]})
        src_path = _write_source(tmp_path, src)
        config = _config(
            src_path,
            {"validators": [{"name": "luhn", "columns": {"t": ["cc"]}}]},
        )
        try:
            run_pipeline(config, _sources(src_path), engine_version=_ENGINE_VERSION)
            pytest.fail("Expected ValidatorFailedError")
        except ValidatorFailedError as exc:
            assert exc.report is not None
            assert exc.report.passed is False
            assert len(exc.report.findings) == 1
            assert exc.report.findings[0].validator == "luhn"
            assert exc.report.findings[0].column == "cc"
