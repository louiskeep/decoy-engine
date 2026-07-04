"""TDD tests for QuarantineConfig validation and apply_quarantine fail-closed guard.

Written BEFORE the production fixes per the CLAUDE.md "land the assertion test
first" rule.

Covers:
  B1 - config: enabled=True with empty output_path raises at Pydantic validation.
  B1 - backstop: apply_quarantine raises if it has rows to write but output_path
       is absent (defence-in-depth for callers who bypass Pydantic).
  M2 - config: triggers containing an unimplemented name (format_error, mask_error)
       raise at Pydantic validation with a clear message naming the trigger.
  M2 - config: triggers: [validation_fail] is accepted cleanly.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from pydantic import ValidationError

from decoy_engine.config._validators import QuarantineConfig
from decoy_engine.quarantine import apply_quarantine
from decoy_engine.validators._types import ValidationReport, ValidatorFinding

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(
    table: str,
    failing: tuple[int, ...],
    validator: str = "luhn",
) -> ValidationReport:
    finding = ValidatorFinding(
        validator=validator,
        table=table,
        column="cc",
        failing_row_indices=failing,
        detail="test finding",
    )
    return ValidationReport(
        passed=False,
        validators_run=(validator,),
        findings=(finding,),
        elapsed_ms=0.0,
    )


def _simple_table() -> pa.Table:
    return pa.table({"cc": ["4532015112830367", "4111111111111111"]})


# ---------------------------------------------------------------------------
# B1 - config-level: enabled=True + empty output_path must raise
# ---------------------------------------------------------------------------


class TestB1ConfigValidation:
    """QuarantineConfig raises when enabled is True but output_path is empty."""

    def test_empty_output_path_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            QuarantineConfig(enabled=True, output_path="", triggers=["validation_fail"])
        assert "output_path" in str(exc_info.value).lower()

    def test_whitespace_output_path_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            QuarantineConfig(enabled=True, output_path="   ", triggers=["validation_fail"])
        assert "output_path" in str(exc_info.value).lower()

    def test_missing_output_path_raises(self) -> None:
        """No output_path field at all (defaults to empty string) must raise when enabled."""
        with pytest.raises(ValidationError) as exc_info:
            QuarantineConfig(enabled=True, triggers=["validation_fail"])
        assert "output_path" in str(exc_info.value).lower()

    def test_valid_output_path_accepted(self, tmp_path: Any) -> None:
        """A non-empty output_path with enabled=True must not raise."""
        cfg = QuarantineConfig(
            enabled=True,
            output_path=str(tmp_path / "out.jsonl"),
            triggers=["validation_fail"],
        )
        assert cfg.enabled is True

    def test_disabled_empty_output_path_ok(self) -> None:
        """When enabled=False, output_path may be empty (legacy / disabled config)."""
        cfg = QuarantineConfig(enabled=False, output_path="", triggers=[])
        assert cfg.enabled is False


# ---------------------------------------------------------------------------
# B1 - backstop: apply_quarantine raises when rows are present but path is empty
# ---------------------------------------------------------------------------


class TestB1Backstop:
    """apply_quarantine raises instead of silently dropping rows when output_path is empty."""

    def test_raises_when_rows_and_no_path(self) -> None:
        table = _simple_table()
        outputs = {"t": table}
        report = _make_report("t", (0,))  # row 0 fails
        quarantine_cfg: dict[str, Any] = {
            "enabled": True,
            "output_path": "",
            "triggers": ["validation_fail"],
        }
        with pytest.raises((ValueError, RuntimeError)) as exc_info:
            apply_quarantine(outputs, report, quarantine_cfg)
        msg = str(exc_info.value).lower()
        assert "output_path" in msg or "quarantine" in msg

    def test_no_raise_when_no_rows(self) -> None:
        """When no rows are quarantined, the empty-path backstop does not fire."""
        table = pa.table({"cc": ["4111111111111111"]})
        outputs = {"t": table}
        # Report with no failing rows
        report = ValidationReport(
            passed=True,
            validators_run=(),
            findings=(),
            elapsed_ms=0.0,
        )
        quarantine_cfg: dict[str, Any] = {
            "enabled": True,
            "output_path": "",
            "triggers": ["validation_fail"],
        }
        # Must not raise - nothing to write
        filtered, _summary = apply_quarantine(outputs, report, quarantine_cfg)
        assert filtered["t"].num_rows == 1


# ---------------------------------------------------------------------------
# M2 - triggers: unimplemented names must raise at config validation
# ---------------------------------------------------------------------------


class TestM2TriggerValidation:
    """Unimplemented triggers are rejected at config validation.

    Sprint 2 honesty pack: format_error was wired in S5 (bucketize +
    date_shift row-error producers) and mask_error in S6 (code_set
    row-error producer). Both are now accepted triggers; only a genuinely
    unknown trigger name still raises.
    """

    def test_format_error_trigger_accepted(self) -> None:
        cfg = QuarantineConfig(
            enabled=True,
            output_path="/tmp/q.jsonl",
            triggers=["format_error"],
        )
        assert cfg.triggers == ["format_error"]

    def test_mask_error_trigger_accepted(self) -> None:
        cfg = QuarantineConfig(
            enabled=True,
            output_path="/tmp/q.jsonl",
            triggers=["mask_error"],
        )
        assert cfg.triggers == ["mask_error"]

    def test_unknown_trigger_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            QuarantineConfig(
                enabled=True,
                output_path="/tmp/q.jsonl",
                triggers=["totally_unknown"],
            )
        assert "totally_unknown" in str(exc_info.value)

    def test_validation_fail_accepted(self) -> None:
        """validation_fail is the only wired trigger and must be accepted."""
        cfg = QuarantineConfig(
            enabled=True,
            output_path="/tmp/q.jsonl",
            triggers=["validation_fail"],
        )
        assert cfg.triggers == ["validation_fail"]

    def test_empty_triggers_accepted_when_disabled(self) -> None:
        """disabled config with empty triggers is always fine."""
        cfg = QuarantineConfig(enabled=False, output_path="", triggers=[])
        assert not cfg.enabled

    def test_empty_triggers_accepted_when_enabled(self, tmp_path: Any) -> None:
        """enabled config with empty triggers list (no triggers) and valid path must be accepted."""
        cfg = QuarantineConfig(
            enabled=True,
            output_path=str(tmp_path / "q.jsonl"),
            triggers=[],
        )
        assert cfg.enabled is True
