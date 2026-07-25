"""Per-validator unit tests: happy and deny-path (sad) for all 6 validators.

TDD: these tests were written before the validator implementation.
Each validator must:
  - Pass (ValidationReport.passed=True) when output values are valid.
  - Fail (ValidationReport.passed=False, findings non-empty) when output
    values are invalid (deny path).

Check-digit validators (luhn, npi, iban, vin) reuse checksums.validate().
FK validators (fk_intact, no_orphan_children) implement parent-first DAG
semantics per the SDV HMA1 pattern.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.validators import validate
from decoy_engine.validators._types import ValidationReport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(outputs: dict[str, pa.Table], config: dict[str, Any]) -> ValidationReport:
    return validate(outputs, config)


# ---------------------------------------------------------------------------
# luhn
# ---------------------------------------------------------------------------


class TestLuhnValidator:
    """luhn: validates Luhn-checksum integrity per column."""

    _VALID_CC = "4532015112830366"  # Visa test card; Luhn-valid
    _INVALID_CC = "4532015112830367"  # last digit flipped; Luhn-invalid

    def test_valid_column_passes(self) -> None:
        outputs = {"t": pa.table({"cc": [self._VALID_CC, "4111111111111111"]})}
        config: dict[str, Any] = {"validators": [{"name": "luhn", "columns": {"t": ["cc"]}}]}
        report = _run(outputs, config)
        assert report.passed is True
        assert report.findings == ()

    def test_invalid_value_fails(self) -> None:
        """deny path: one bad Luhn value in the column fails the validator."""
        outputs = {"t": pa.table({"cc": [self._VALID_CC, self._INVALID_CC]})}
        config: dict[str, Any] = {"validators": [{"name": "luhn", "columns": {"t": ["cc"]}}]}
        report = _run(outputs, config)
        assert report.passed is False
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert finding.validator == "luhn"
        assert finding.table == "t"
        assert finding.column == "cc"
        assert 1 in finding.failing_row_indices  # row index 1 is bad

    def test_null_values_are_skipped(self) -> None:
        """Null FK values are not checked (not a Luhn violation)."""
        outputs = {"t": pa.table({"cc": [None, self._VALID_CC]})}
        config: dict[str, Any] = {"validators": [{"name": "luhn", "columns": {"t": ["cc"]}}]}
        report = _run(outputs, config)
        assert report.passed is True

    def test_validators_run_records_name(self) -> None:
        outputs = {"t": pa.table({"cc": [self._VALID_CC]})}
        config: dict[str, Any] = {"validators": [{"name": "luhn", "columns": {"t": ["cc"]}}]}
        report = _run(outputs, config)
        assert "luhn" in report.validators_run


# ---------------------------------------------------------------------------
# npi
# ---------------------------------------------------------------------------


class TestNpiValidator:
    """npi: validates NPI mod-10 check digit per column."""

    _VALID_NPI = "1234567893"  # CMS NPPES spec example; valid
    _INVALID_NPI = "1234567890"  # same body, wrong check digit

    def test_valid_column_passes(self) -> None:
        outputs = {"p": pa.table({"npi": [self._VALID_NPI]})}
        config: dict[str, Any] = {"validators": [{"name": "npi", "columns": {"p": ["npi"]}}]}
        report = _run(outputs, config)
        assert report.passed is True

    def test_invalid_value_fails(self) -> None:
        """deny path: an NPI with a wrong check digit fails the validator."""
        outputs = {"p": pa.table({"npi": [self._INVALID_NPI]})}
        config: dict[str, Any] = {"validators": [{"name": "npi", "columns": {"p": ["npi"]}}]}
        report = _run(outputs, config)
        assert report.passed is False
        assert len(report.findings) == 1
        assert report.findings[0].validator == "npi"

    def test_null_values_are_skipped(self) -> None:
        outputs = {"p": pa.table({"npi": [None]})}
        config: dict[str, Any] = {"validators": [{"name": "npi", "columns": {"p": ["npi"]}}]}
        report = _run(outputs, config)
        assert report.passed is True


# ---------------------------------------------------------------------------
# iban
# ---------------------------------------------------------------------------


class TestIbanValidator:
    """iban: validates IBAN mod-97 per column."""

    _VALID_IBAN = "GB82WEST12345698765432"  # SWIFT spec example
    _INVALID_IBAN = "GB00WEST12345698765432"  # check digits 00 are always invalid

    def test_valid_column_passes(self) -> None:
        outputs = {"pay": pa.table({"iban": [self._VALID_IBAN]})}
        config: dict[str, Any] = {"validators": [{"name": "iban", "columns": {"pay": ["iban"]}}]}
        report = _run(outputs, config)
        assert report.passed is True

    def test_invalid_value_fails(self) -> None:
        """deny path: an IBAN with wrong check digits fails the validator."""
        outputs = {"pay": pa.table({"iban": [self._INVALID_IBAN]})}
        config: dict[str, Any] = {"validators": [{"name": "iban", "columns": {"pay": ["iban"]}}]}
        report = _run(outputs, config)
        assert report.passed is False
        assert report.findings[0].validator == "iban"

    def test_null_values_are_skipped(self) -> None:
        outputs = {"pay": pa.table({"iban": [None]})}
        config: dict[str, Any] = {"validators": [{"name": "iban", "columns": {"pay": ["iban"]}}]}
        report = _run(outputs, config)
        assert report.passed is True


# ---------------------------------------------------------------------------
# vin
# ---------------------------------------------------------------------------


class TestVinValidator:
    """vin: validates VIN ISO 3779 check digit per column."""

    _VALID_VIN = (
        "1HGBH41JXMN109186"  # NHTSA example; check digit X at pos 8 -> but let's pick valid
    )
    # Known valid VIN (verified via checksums.validate); invalid variant
    # has the last digit changed so the ISO 3779 check character fails.
    _VALID_VIN = "1HGBH41JXMN109186"
    _INVALID_VIN = "1HGBH41JXMN109187"  # last digit changed; check character fails

    def test_valid_column_passes(self) -> None:
        outputs = {"v": pa.table({"vin": [self._VALID_VIN]})}
        config: dict[str, Any] = {"validators": [{"name": "vin", "columns": {"v": ["vin"]}}]}
        report = _run(outputs, config)
        assert report.passed is True

    def test_invalid_value_fails(self) -> None:
        """deny path: a VIN with a wrong check character fails the validator."""
        outputs = {"v": pa.table({"vin": [self._INVALID_VIN]})}
        config: dict[str, Any] = {"validators": [{"name": "vin", "columns": {"v": ["vin"]}}]}
        report = _run(outputs, config)
        assert report.passed is False
        assert report.findings[0].validator == "vin"

    def test_null_values_are_skipped(self) -> None:
        outputs = {"v": pa.table({"vin": [None]})}
        config: dict[str, Any] = {"validators": [{"name": "vin", "columns": {"v": ["vin"]}}]}
        report = _run(outputs, config)
        assert report.passed is True


# ---------------------------------------------------------------------------
# fk_intact
# ---------------------------------------------------------------------------


class TestFkIntactValidator:
    """fk_intact: every non-null child FK value resolves to a parent PK."""

    def test_valid_fk_passes(self) -> None:
        """All child FKs exist in the parent PK set."""
        outputs = {
            "orders": pa.table({"id": ["1", "2", "3"]}),
            "items": pa.table({"order_id": ["1", "2", "3"]}),
        }
        config: dict[str, Any] = {
            "validators": [{"name": "fk_intact"}],
            "relationships": [
                {
                    "parent": {"table": "orders", "columns": ["id"]},
                    "children": [{"table": "items", "columns": ["order_id"]}],
                    "orphan_policy": "fail",
                }
            ],
        }
        report = _run(outputs, config)
        assert report.passed is True

    def test_broken_fk_fails(self) -> None:
        """deny path: a child FK value that has no parent PK fails the validator."""
        outputs = {
            "orders": pa.table({"id": ["1", "2"]}),
            "items": pa.table({"order_id": ["1", "99"]}),  # 99 has no parent
        }
        config: dict[str, Any] = {
            "validators": [{"name": "fk_intact"}],
            "relationships": [
                {
                    "parent": {"table": "orders", "columns": ["id"]},
                    "children": [{"table": "items", "columns": ["order_id"]}],
                    "orphan_policy": "fail",
                }
            ],
        }
        report = _run(outputs, config)
        assert report.passed is False
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert finding.validator == "fk_intact"

    def test_null_child_fk_not_a_violation(self) -> None:
        """fk_intact does not flag null FKs as broken references."""
        outputs = {
            "orders": pa.table({"id": ["1"]}),
            "items": pa.table({"order_id": [None]}),
        }
        config: dict[str, Any] = {
            "validators": [{"name": "fk_intact"}],
            "relationships": [
                {
                    "parent": {"table": "orders", "columns": ["id"]},
                    "children": [{"table": "items", "columns": ["order_id"]}],
                    "orphan_policy": "fail",
                }
            ],
        }
        report = _run(outputs, config)
        assert report.passed is True

    def test_no_relationships_always_passes(self) -> None:
        outputs = {"t": pa.table({"x": ["a"]})}
        config: dict[str, Any] = {"validators": [{"name": "fk_intact"}]}
        report = _run(outputs, config)
        assert report.passed is True


# ---------------------------------------------------------------------------
# no_orphan_children
# ---------------------------------------------------------------------------


class TestNoOrphanChildrenValidator:
    """no_orphan_children: every child row has a non-null FK (no orphaned children)."""

    def test_all_children_have_parent_fk_passes(self) -> None:
        """All children have non-null FKs; validator passes."""
        outputs = {
            "orders": pa.table({"id": ["1", "2"]}),
            "items": pa.table({"order_id": ["1", "2"]}),
        }
        config: dict[str, Any] = {
            "validators": [{"name": "no_orphan_children"}],
            "relationships": [
                {
                    "parent": {"table": "orders", "columns": ["id"]},
                    "children": [{"table": "items", "columns": ["order_id"]}],
                    "orphan_policy": "fail",
                }
            ],
        }
        report = _run(outputs, config)
        assert report.passed is True

    def test_null_child_fk_fails(self) -> None:
        """deny path: a child row with a null FK is an orphan; validator fails."""
        outputs = {
            "orders": pa.table({"id": ["1", "2"]}),
            "items": pa.table({"order_id": ["1", None]}),  # row 1 is orphaned
        }
        config: dict[str, Any] = {
            "validators": [{"name": "no_orphan_children"}],
            "relationships": [
                {
                    "parent": {"table": "orders", "columns": ["id"]},
                    "children": [{"table": "items", "columns": ["order_id"]}],
                    "orphan_policy": "fail",
                }
            ],
        }
        report = _run(outputs, config)
        assert report.passed is False
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert finding.validator == "no_orphan_children"
        assert 1 in finding.failing_row_indices

    def test_no_relationships_always_passes(self) -> None:
        outputs = {"t": pa.table({"x": ["a"]})}
        config: dict[str, Any] = {"validators": [{"name": "no_orphan_children"}]}
        report = _run(outputs, config)
        assert report.passed is True


# ---------------------------------------------------------------------------
# Unknown validator
# ---------------------------------------------------------------------------


class TestUnknownValidator:
    """An unrecognised validator name raises a clear error at runtime."""

    def test_unknown_validator_raises(self) -> None:
        outputs = {"t": pa.table({"x": ["1"]})}
        config: dict[str, Any] = {"validators": [{"name": "bogus_validator"}]}
        with pytest.raises(ValueError, match="bogus_validator"):
            validate(outputs, config)


# ---------------------------------------------------------------------------
# Multiple validators in one run
# ---------------------------------------------------------------------------


class TestMultipleValidators:
    """The framework runs all configured validators and aggregates findings."""

    def test_all_pass_when_data_valid(self) -> None:
        outputs = {"t": pa.table({"cc": ["4532015112830366"], "npi": ["1234567893"]})}
        config: dict[str, Any] = {
            "validators": [
                {"name": "luhn", "columns": {"t": ["cc"]}},
                {"name": "npi", "columns": {"t": ["npi"]}},
            ]
        }
        report = _run(outputs, config)
        assert report.passed is True
        assert set(report.validators_run) == {"luhn", "npi"}

    def test_one_fail_makes_report_fail(self) -> None:
        """Even one validator failure makes the whole report fail."""
        outputs = {"t": pa.table({"cc": ["4532015112830367"], "npi": ["1234567893"]})}
        config: dict[str, Any] = {
            "validators": [
                {"name": "luhn", "columns": {"t": ["cc"]}},  # bad luhn
                {"name": "npi", "columns": {"t": ["npi"]}},  # valid npi
            ]
        }
        report = _run(outputs, config)
        assert report.passed is False
        assert len(report.findings) == 1

    def test_elapsed_ms_is_non_negative(self) -> None:
        outputs = {"t": pa.table({"cc": ["4532015112830366"]})}
        config: dict[str, Any] = {"validators": [{"name": "luhn", "columns": {"t": ["cc"]}}]}
        report = _run(outputs, config)
        assert report.elapsed_ms >= 0.0
