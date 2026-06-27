"""SP-05 quarantine integration test.

Verifies that:
  - Rows failing a validator land in the quarantine output when quarantine
    is enabled with the ``validation_fail`` trigger.
  - The main output excludes the quarantined rows.
  - The job completes successfully (no ValidatorFailedError).
  - The evidence manifest summarises quarantine state (counts per trigger,
    output path).
  - M1: a row failing two validators appears ONCE in the quarantine file,
    total_quarantined == 1, and 1 row is removed from main (not 2).
  - L2: the quality_metrics dict (evidence manifest) round-trips to disk
    and back, with validation.validators and quarantine both correct.

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


# ---------------------------------------------------------------------------
# M1 - duplicate-validator dedup: one row failing two validators -> 1 record
# ---------------------------------------------------------------------------


def _config_two_validators(
    src_path: str,
    quarantine_path: str,
) -> dict[str, Any]:
    """Config with both luhn AND npi validators on the same column.

    Row 0 has a value that fails Luhn. The same value is not a valid NPI
    either (NPI uses Luhn too, but with a different prefix expectation).
    Presence of two validators ensures two findings reference the same row.
    """
    return {
        "version": 1,
        "global_settings": {"job_name": "sp05-m1-dedup-test", "seed": 42},
        "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
        "targets": {"t": {"type": "file", "path": src_path + ".out", "format": "parquet"}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "cc", "strategy": "passthrough"},
                    {"name": "npi", "strategy": "passthrough"},
                ],
            }
        ],
        "relationships": [],
        # Both validators target the same row (row 0: bad luhn + bad npi)
        "validators": [
            {"name": "luhn", "columns": {"t": ["cc"]}},
            {"name": "npi", "columns": {"t": ["npi"]}},
        ],
        "quarantine": {
            "enabled": True,
            "output_path": quarantine_path,
            "triggers": ["validation_fail"],
        },
    }


class TestM1DuplicateValidatorDedup:
    """A row that fails two validators appears exactly once in the quarantine file.

    M1 spec: total_quarantined == distinct rows removed from main (not
    the sum of per-finding counts). Per-trigger counts may sum higher.
    """

    def test_one_row_two_validators_one_quarantine_record(self, tmp_path: Path) -> None:
        """Row 0 fails both luhn and npi: quarantine file has 1 line, not 2."""
        # "4532015112830367" fails Luhn; "1234567890" fails NPI check.
        src = pa.table(
            {
                "cc": ["4532015112830367", "4111111111111111"],
                "npi": ["1234567890", "1234567893"],
            }
        )
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config_two_validators(src_path, qpath)

        result = run_pipeline(config, _sources(src_path), engine_version="0.1.0")

        # Main output should have exactly 1 row (row 1 passed both validators).
        assert result.outputs["t"].num_rows == 1, (
            "row 0 should have been removed from main output exactly once"
        )

        # Quarantine file: exactly 1 line for the 1 distinct quarantined row.
        assert Path(qpath).exists()
        lines = Path(qpath).read_text().splitlines()
        assert len(lines) == 1, f"expected 1 quarantine record, got {len(lines)}"

        # Manifest: total_quarantined == distinct rows (1), not sum of per-trigger
        # counts (which may be 2 if counts_by_trigger tallies per finding).
        summary = result.quality_metrics["quarantine"]
        assert summary["total_quarantined"] == 1, (
            f"total_quarantined should be 1 (distinct rows), got {summary['total_quarantined']}"
        )


# ---------------------------------------------------------------------------
# L2 - persisted manifest round-trip
# ---------------------------------------------------------------------------


class TestL2PersistedManifestRoundTrip:
    """quality_metrics round-trips to JSON on disk and back with all keys intact.

    Simulates what the platform manifest writer does: serialise
    ExecutionResult.quality_metrics to JSON, persist to disk, reload,
    and assert the key sections are present and structurally correct.
    """

    def test_manifest_round_trips_with_validation_and_quarantine(self, tmp_path: Path) -> None:
        src = pa.table({"cc": ["4111111111111111", "4532015112830367"]})
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(src_path, qpath)

        result = run_pipeline(config, _sources(src_path), engine_version="0.1.0")

        # Persist quality_metrics to disk (as the platform manifest writer does).
        manifest_path = tmp_path / "quality_metrics.json"
        manifest_path.write_text(json.dumps(result.quality_metrics, default=str), encoding="utf-8")

        # Round-trip: reload from disk.
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))

        # validation block must be present and carry validators_run.
        # The report reflects that findings were produced (passed=False)
        # even though the job succeeded via quarantine.
        assert "validation" in loaded, "manifest missing 'validation' key"
        val = loaded["validation"]
        assert "validators" in val, "validation block missing 'validators' key"
        assert (
            val["validators"]["passed"] is False
        )  # findings were produced; quarantine handled them
        assert "luhn" in val["validators"]["validators_run"]
        assert isinstance(val["validators"]["findings"], list)
        assert len(val["validators"]["findings"]) >= 1

        # quarantine block must be present and accurate.
        assert "quarantine" in loaded, "manifest missing 'quarantine' key"
        q = loaded["quarantine"]
        assert q["total_quarantined"] == 1
        assert q["counts_by_trigger"]["validation_fail"] == 1
        assert q["output_path"] == qpath
        assert q["enabled"] is True
