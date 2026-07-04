"""S5 (Sprint 2 honesty pack, D9): apply_quarantine generalized to accept
row_errors alongside the ValidationReport.

TDD: written before the implementation. `apply_quarantine` builds one
normalized worklist of (table, row_index, trigger, reason) from validator
findings (tagged "validation_fail") and row-error records (tagged with their
own trigger), then runs the EXISTING dedup/write/filter machinery once.
Existing validation_fail-only behavior must stay byte-identical (regression
pin: the existing tests/integration/test_quarantine_e2e.py suite passes
unchanged with row_errors=() default).
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa

from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.quarantine import apply_quarantine
from decoy_engine.validators._types import ValidationReport


def _empty_report() -> ValidationReport:
    return ValidationReport(passed=True, validators_run=(), findings=(), elapsed_ms=0.0)


class TestRowErrorsOnlyQuarantine:
    def test_format_error_rows_removed_and_written(self, tmp_path: Path) -> None:
        table = pa.table({"age": ["23", "bad", "47"]})
        outputs = {"t": table}
        row_errors = (
            RowErrorRecord(
                table="t", column="age", row_index=1, trigger="format_error", reason="not numeric"
            ),
        )
        qpath = str(tmp_path / "q.jsonl")
        quarantine_cfg = {"enabled": True, "output_path": qpath, "triggers": ["format_error"]}
        filtered, summary = apply_quarantine(
            outputs, _empty_report(), quarantine_cfg, row_errors=row_errors
        )
        assert filtered["t"].num_rows == 2
        assert filtered["t"].column("age").to_pylist() == ["23", "47"]
        assert summary.total_quarantined == 1
        assert summary.counts_by_trigger == {"format_error": 1}
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert records[0]["age"] == "bad"
        assert records[0]["_quarantine_trigger"] == "format_error"
        assert records[0]["_quarantine_reason"] == "not numeric"

    def test_trigger_not_enabled_row_stays_in_main_output(self, tmp_path: Path) -> None:
        """A row-error trigger NOT in quarantine.triggers is not quarantined."""
        table = pa.table({"age": ["23", "bad"]})
        outputs = {"t": table}
        row_errors = (
            RowErrorRecord(table="t", column="age", row_index=1, trigger="mask_error", reason="x"),
        )
        quarantine_cfg = {
            "enabled": True,
            "output_path": str(tmp_path / "q.jsonl"),
            "triggers": ["format_error"],  # mask_error NOT enabled
        }
        filtered, summary = apply_quarantine(
            outputs, _empty_report(), quarantine_cfg, row_errors=row_errors
        )
        assert filtered["t"].num_rows == 2  # nothing removed
        assert summary.total_quarantined == 0


class TestMixedValidatorAndRowErrors:
    def test_same_row_both_triggers_dedup_and_per_trigger_counts(self, tmp_path: Path) -> None:
        from decoy_engine.validators._types import ValidatorFinding

        table = pa.table({"cc": ["4111111111111111", "4532015112830367"]})
        outputs = {"t": table}
        report = ValidationReport(
            passed=False,
            validators_run=("luhn",),
            findings=(
                ValidatorFinding(
                    validator="luhn",
                    table="t",
                    column="cc",
                    failing_row_indices=(1,),
                    detail="bad luhn",
                ),
            ),
            elapsed_ms=0.0,
        )
        row_errors = (
            RowErrorRecord(
                table="t", column="cc", row_index=1, trigger="format_error", reason="also bad"
            ),
        )
        qpath = str(tmp_path / "q.jsonl")
        quarantine_cfg = {
            "enabled": True,
            "output_path": qpath,
            "triggers": ["validation_fail", "format_error"],
        }
        filtered, summary = apply_quarantine(outputs, report, quarantine_cfg, row_errors=row_errors)
        assert filtered["t"].num_rows == 1  # row 1 removed exactly once
        assert summary.total_quarantined == 1
        assert summary.counts_by_trigger == {"validation_fail": 1, "format_error": 1}
        lines = Path(qpath).read_text().splitlines()
        assert len(lines) == 1  # deduped: one JSONL line for the one distinct row


class TestRegressionPinValidationFailOnlyUnchanged:
    def test_default_row_errors_empty_byte_identical(self, tmp_path: Path) -> None:
        from decoy_engine.validators._types import ValidatorFinding

        table = pa.table({"cc": ["4111111111111111", "4532015112830367"]})
        outputs = {"t": table}
        report = ValidationReport(
            passed=False,
            validators_run=("luhn",),
            findings=(
                ValidatorFinding(
                    validator="luhn", table="t", column="cc", failing_row_indices=(1,), detail="bad"
                ),
            ),
            elapsed_ms=0.0,
        )
        quarantine_cfg = {
            "enabled": True,
            "output_path": str(tmp_path / "q.jsonl"),
            "triggers": ["validation_fail"],
        }
        filtered, summary = apply_quarantine(outputs, report, quarantine_cfg)  # no row_errors kwarg
        assert filtered["t"].num_rows == 1
        assert summary.total_quarantined == 1
        assert summary.counts_by_trigger == {"validation_fail": 1}
