"""SP-05 quarantine integration test.

Verifies that:
  - Rows failing a validator land in the quarantine output when quarantine
    is enabled with the ``validation_fail`` trigger.
  - The main output excludes the quarantined rows.
  - The job completes successfully (no ValidatorFailedError).
  - The evidence manifest summarises quarantine state (counts per trigger,
    output path).

The quarantine output is a JSON-lines file. Each record carries the original
row data plus ``_quarantine_trigger``, ``_quarantine_reason``, and
``_source_table`` extra fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._pipeline import run_pipeline


def _write_source(tmp_path: Path, table: pa.Table, name: str = "t") -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _sources(src_path: str) -> dict[str, pa.Table]:
    return {"t": pq.read_table(src_path)}


def _config(
    src_path: str,
    quarantine_path: str,
    triggers: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "global_settings": {"job_name": "sp05-quarantine-test", "seed": 42},
        "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
        "targets": {"t": {"type": "file", "path": src_path + ".out", "format": "parquet"}},
        "tables": [
            {
                "name": "t",
                "columns": [{"name": "cc", "strategy": "passthrough"}],
            }
        ],
        "relationships": [],
        "validators": [{"name": "luhn", "columns": {"t": ["cc"]}}],
        "quarantine": {
            "enabled": True,
            "output_path": quarantine_path,
            "triggers": triggers or ["validation_fail"],
        },
    }


class TestQuarantineValidationFail:
    """Rows failing a validator are quarantined; the job succeeds."""

    def test_failing_rows_removed_from_main_output(self, tmp_path: Path) -> None:
        """The main output contains only the rows that passed validation."""
        src = pa.table({"cc": ["4111111111111111", "4532015112830367"]})
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(src_path, qpath)
        result = run_pipeline(config, _sources(src_path), engine_version="0.1.0")
        out = result.outputs["t"]
        assert out.num_rows == 1
        assert out.column("cc").to_pylist() == ["4111111111111111"]

    def test_failing_rows_in_quarantine_file(self, tmp_path: Path) -> None:
        """The quarantine file exists and contains the bad row."""
        src = pa.table({"cc": ["4111111111111111", "4532015112830367"]})
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(src_path, qpath)
        run_pipeline(config, _sources(src_path), engine_version="0.1.0")
        assert Path(qpath).exists()
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["cc"] == "4532015112830367"
        assert records[0]["_quarantine_trigger"] == "validation_fail"

    def test_job_succeeds_no_exception(self, tmp_path: Path) -> None:
        """When quarantine is enabled with validation_fail trigger, no exception is raised."""
        src = pa.table({"cc": ["4532015112830367"]})  # all rows bad
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(src_path, qpath)
        # Must NOT raise ValidatorFailedError
        result = run_pipeline(config, _sources(src_path), engine_version="0.1.0")
        # All rows were quarantined; main output is empty
        assert result.outputs["t"].num_rows == 0

    def test_quarantine_summary_in_manifest(self, tmp_path: Path) -> None:
        """quality_metrics["quarantine"] carries counts per trigger and the path."""
        src = pa.table({"cc": ["4111111111111111", "4532015112830367"]})
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(src_path, qpath)
        result = run_pipeline(config, _sources(src_path), engine_version="0.1.0")
        assert "quarantine" in result.quality_metrics
        summary = result.quality_metrics["quarantine"]
        assert summary["total_quarantined"] == 1
        assert summary["counts_by_trigger"]["validation_fail"] == 1
        assert summary["output_path"] == qpath

    def test_all_valid_no_quarantine_file_written(self, tmp_path: Path) -> None:
        """When no rows fail, the quarantine file is not created."""
        src = pa.table({"cc": ["4111111111111111"]})
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(src_path, qpath)
        run_pipeline(config, _sources(src_path), engine_version="0.1.0")
        assert not Path(qpath).exists()


class TestQuarantineDisabled:
    """When quarantine is disabled, the original fail-closed behaviour is preserved."""

    def test_quarantine_disabled_fails_job(self, tmp_path: Path) -> None:
        from decoy_engine.errors import ValidatorFailedError

        src = pa.table({"cc": ["4532015112830367"]})
        src_path = _write_source(tmp_path, src)

        config: dict[str, Any] = {
            "version": 1,
            "global_settings": {"job_name": "sp05-quarantine-off", "seed": 42},
            "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
            "targets": {
                "t": {
                    "type": "file",
                    "path": src_path + ".out",
                    "format": "parquet",
                }
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [{"name": "cc", "strategy": "passthrough"}],
                }
            ],
            "relationships": [],
            "validators": [{"name": "luhn", "columns": {"t": ["cc"]}}],
            "quarantine": {
                "enabled": False,
                "output_path": "/dev/null",
                "triggers": ["validation_fail"],
            },
        }
        with pytest.raises(ValidatorFailedError):
            run_pipeline(config, _sources(src_path), engine_version="0.1.0")
