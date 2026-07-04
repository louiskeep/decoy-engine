"""S3 (Sprint 2 honesty pack): regex_match + column_in_set generic validators.

TDD: written before the implementation.

regex_match: Great Expectations `expect_column_values_to_match_regex` semantics
(whole-cell `re.fullmatch`). column_in_set: GE
`expect_column_values_to_be_in_set` semantics (str-canonicalized membership).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.validators import validate


class TestRegexMatch:
    def test_all_match_passes(self) -> None:
        outputs = {"t": pa.table({"code": ["AB123", "CD456"]})}
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "regex_match",
                    "columns": {"t": ["code"]},
                    "params": {"pattern": r"[A-Z]{2}\d{3}"},
                }
            ]
        }
        report = validate(outputs, config)
        assert report.passed is True

    def test_non_matching_value_fails_with_row_index(self) -> None:
        outputs = {"t": pa.table({"code": ["AB123", "bad-value"]})}
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "regex_match",
                    "columns": {"t": ["code"]},
                    "params": {"pattern": r"[A-Z]{2}\d{3}"},
                }
            ]
        }
        report = validate(outputs, config)
        assert report.passed is False
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert finding.validator == "regex_match"
        assert finding.column == "code"
        assert finding.failing_row_indices == (1,)

    def test_nulls_skipped(self) -> None:
        outputs = {"t": pa.table({"code": [None, "AB123"]})}
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "regex_match",
                    "columns": {"t": ["code"]},
                    "params": {"pattern": r"[A-Z]{2}\d{3}"},
                }
            ]
        }
        report = validate(outputs, config)
        assert report.passed is True

    def test_fullmatch_semantics_substring_does_not_pass(self) -> None:
        """Match rule is re.fullmatch, not re.search: a partial match fails."""
        outputs = {"t": pa.table({"code": ["xxAB123xx"]})}
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "regex_match",
                    "columns": {"t": ["code"]},
                    "params": {"pattern": r"[A-Z]{2}\d{3}"},
                }
            ]
        }
        report = validate(outputs, config)
        assert report.passed is False

    def test_missing_pattern_raises_value_error(self) -> None:
        outputs = {"t": pa.table({"code": ["AB123"]})}
        config: dict[str, Any] = {
            "validators": [{"name": "regex_match", "columns": {"t": ["code"]}, "params": {}}]
        }
        with pytest.raises(ValueError, match="regex_match"):
            validate(outputs, config)

    def test_bad_pattern_raises_value_error(self) -> None:
        outputs = {"t": pa.table({"code": ["AB123"]})}
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "regex_match",
                    "columns": {"t": ["code"]},
                    "params": {"pattern": "[unclosed"},
                }
            ]
        }
        with pytest.raises(ValueError, match="regex_match"):
            validate(outputs, config)


class TestColumnInSet:
    def test_all_in_set_passes(self) -> None:
        outputs = {"t": pa.table({"status": ["active", "inactive"]})}
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "column_in_set",
                    "columns": {"t": ["status"]},
                    "params": {"allowed_values": ["active", "inactive", "pending"]},
                }
            ]
        }
        report = validate(outputs, config)
        assert report.passed is True

    def test_value_outside_set_fails(self) -> None:
        outputs = {"t": pa.table({"status": ["active", "bogus"]})}
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "column_in_set",
                    "columns": {"t": ["status"]},
                    "params": {"allowed_values": ["active", "inactive"]},
                }
            ]
        }
        report = validate(outputs, config)
        assert report.passed is False
        assert report.findings[0].failing_row_indices == (1,)

    def test_nulls_skipped_by_default(self) -> None:
        outputs = {"t": pa.table({"status": [None, "active"]})}
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "column_in_set",
                    "columns": {"t": ["status"]},
                    "params": {"allowed_values": ["active"]},
                }
            ]
        }
        report = validate(outputs, config)
        assert report.passed is True

    def test_allow_null_false_flags_nulls(self) -> None:
        outputs = {"t": pa.table({"status": [None, "active"]})}
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "column_in_set",
                    "columns": {"t": ["status"]},
                    "params": {"allowed_values": ["active"], "allow_null": False},
                }
            ]
        }
        report = validate(outputs, config)
        assert report.passed is False
        assert report.findings[0].failing_row_indices == (0,)

    def test_str_canonicalization(self) -> None:
        """Comparison canonicalizes via str(), consistent with check-digit validators."""
        outputs = {"t": pa.table({"n": [1, 2]})}
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "column_in_set",
                    "columns": {"t": ["n"]},
                    "params": {"allowed_values": ["1", "2"]},
                }
            ]
        }
        report = validate(outputs, config)
        assert report.passed is True

    def test_missing_allowed_values_raises(self) -> None:
        outputs = {"t": pa.table({"status": ["active"]})}
        config: dict[str, Any] = {
            "validators": [{"name": "column_in_set", "columns": {"t": ["status"]}, "params": {}}]
        }
        with pytest.raises(ValueError, match="column_in_set"):
            validate(outputs, config)

    def test_empty_allowed_values_raises(self) -> None:
        outputs = {"t": pa.table({"status": ["active"]})}
        config: dict[str, Any] = {
            "validators": [
                {
                    "name": "column_in_set",
                    "columns": {"t": ["status"]},
                    "params": {"allowed_values": []},
                }
            ]
        }
        with pytest.raises(ValueError, match="column_in_set"):
            validate(outputs, config)
