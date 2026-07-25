"""Assertion test: ValidationReport is frozen; validate() never mutates output.

This test MUST exist before the implementation (TDD: land the assertion first).
Per CLAUDE.md: 'Validation never mutates; reports are frozen; land the assertion
test first.'

The FrozenInstanceError test proves ValidationReport cannot be mutated post-
construction. The output-immutability test proves validate() does not modify
the pa.Table objects it receives.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.validators import validate
from decoy_engine.validators._types import ValidationReport


class TestValidationReportIsFrozen:
    """ValidationReport is a frozen dataclass; no attribute may be set post-init."""

    def test_frozen_instance_error_on_setattr(self) -> None:
        """Setting any attribute on a ValidationReport raises FrozenInstanceError."""
        report = ValidationReport(
            passed=True,
            validators_run=("luhn",),
            findings=(),
            elapsed_ms=0.0,
        )
        with pytest.raises(FrozenInstanceError):
            report.passed = False  # type: ignore[misc]

    def test_findings_tuple_is_immutable(self) -> None:
        """findings is a tuple, not a list; in-place append is rejected."""
        report = ValidationReport(
            passed=True,
            validators_run=(),
            findings=(),
            elapsed_ms=0.0,
        )
        with pytest.raises(AttributeError):
            report.findings.append(object())  # type: ignore[attr-defined]


class TestValidateDoesNotMutateOutput:
    """validate() must not modify the pa.Table objects it receives."""

    def _make_outputs(self) -> dict[str, pa.Table]:
        return {
            "orders": pa.table({"id": ["1", "2"], "cc": ["4532015112830366", "4111111111111111"]})
        }

    def _luhn_config(self) -> dict[str, Any]:
        return {"validators": [{"name": "luhn", "columns": {"orders": ["cc"]}}]}

    def test_output_schema_unchanged_after_validate(self) -> None:
        """The schema of the output table is the same before and after validate()."""
        outputs = self._make_outputs()
        schema_before = outputs["orders"].schema
        validate(outputs, self._luhn_config())
        assert outputs["orders"].schema == schema_before

    def test_output_row_count_unchanged_after_validate(self) -> None:
        """The row count of the output table does not change after validate()."""
        outputs = self._make_outputs()
        rows_before = outputs["orders"].num_rows
        validate(outputs, self._luhn_config())
        assert outputs["orders"].num_rows == rows_before

    def test_output_values_unchanged_after_validate(self) -> None:
        """The cell values in the output table are not changed by validate()."""
        outputs = self._make_outputs()
        cc_before = outputs["orders"].column("cc").to_pylist()
        validate(outputs, self._luhn_config())
        assert outputs["orders"].column("cc").to_pylist() == cc_before
