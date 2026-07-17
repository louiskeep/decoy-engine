"""HC-3b: plan-compile check for top_code's bound resolution.

Mirrors `tests/unit/plan/test_check_truncate_bucketize_categorical.py`'s
`TestCheckBucketizeConfig` structure (top_code's fail-closed check is the
direct sibling of bucketize's): a TDD-shaped pair per reject code, plus the
pass cases (valid preset, valid manual cap+over_label, valid floor+
under_label, non-top_code column ignored).
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine.plan._checks_top_code import check_top_code_config
from decoy_engine.plan._errors import PlanCompileError


def _config(provider_config: dict[str, Any], *, column: str = "age") -> dict[str, Any]:
    return {
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": column,
                        "strategy": "top_code",
                        "provider_config": provider_config,
                    }
                ],
            }
        ]
    }


class TestUnresolvedPreset:
    def test_unknown_preset_rejected(self) -> None:
        cfg = _config({"preset": "by_fortnight"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_bounds_unresolvable"
        assert exc.value.path == "tables.t.columns.age.provider_config.preset"

    def test_known_preset_compiles(self) -> None:
        check_top_code_config(_config({"preset": "hipaa_age"}))  # no raise


class TestUnresolvedCap:
    def test_missing_cap_and_preset_rejected(self) -> None:
        cfg = _config({})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_bounds_unresolvable"
        assert exc.value.path == "tables.t.columns.age.provider_config.cap"

    def test_string_cap_rejected(self) -> None:
        cfg = _config({"cap": "89", "over_label": "90+"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_bounds_unresolvable"
        assert exc.value.path == "tables.t.columns.age.provider_config.cap"

    def test_bool_cap_rejected(self) -> None:
        cfg = _config({"cap": True, "over_label": "90+"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_bounds_unresolvable"

    def test_valid_numeric_cap_and_over_label_compiles(self) -> None:
        check_top_code_config(_config({"cap": 89, "over_label": "90+"}))  # no raise

    def test_valid_float_cap_compiles(self) -> None:
        check_top_code_config(_config({"cap": 89.5, "over_label": "90+"}))  # no raise


class TestMissingOverLabel:
    def test_missing_over_label_rejected(self) -> None:
        cfg = _config({"cap": 89})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_missing_over_label"
        assert exc.value.path == "tables.t.columns.age.provider_config.over_label"

    def test_empty_string_over_label_rejected(self) -> None:
        cfg = _config({"cap": 89, "over_label": ""})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_missing_over_label"

    def test_non_string_over_label_rejected(self) -> None:
        cfg = _config({"cap": 89, "over_label": 90})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_missing_over_label"

    def test_preset_ignores_missing_manual_over_label(self) -> None:
        """preset supplies cap+over_label; a manual over_label is not required
        (and, per the guide, any manual cap/over_label would be ignored)."""
        check_top_code_config(_config({"preset": "hipaa_age"}))  # no raise


class TestInvalidFloor:
    def test_string_floor_rejected(self) -> None:
        cfg = _config({"cap": 89, "over_label": "90+", "floor": "0", "under_label": "<0"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_invalid_floor"
        assert exc.value.path == "tables.t.columns.age.provider_config.floor"

    def test_bool_floor_rejected(self) -> None:
        cfg = _config({"cap": 89, "over_label": "90+", "floor": True, "under_label": "<0"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_invalid_floor"

    def test_no_floor_is_not_an_error(self) -> None:
        check_top_code_config(_config({"cap": 89, "over_label": "90+"}))  # no raise


class TestMissingUnderLabel:
    def test_floor_without_under_label_rejected(self) -> None:
        cfg = _config({"cap": 89, "over_label": "90+", "floor": 0})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_missing_under_label"
        assert exc.value.path == "tables.t.columns.age.provider_config.under_label"

    def test_empty_string_under_label_rejected(self) -> None:
        cfg = _config({"cap": 89, "over_label": "90+", "floor": 0, "under_label": ""})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_missing_under_label"

    def test_valid_floor_and_under_label_compiles(self) -> None:
        cfg = _config({"cap": 89, "over_label": "90+", "floor": 0, "under_label": "<0"})
        check_top_code_config(cfg)  # no raise

    def test_valid_floor_and_under_label_with_preset_compiles(self) -> None:
        """floor/under_label may be supplied alongside a preset (the preset
        only sets the top bound)."""
        cfg = _config({"preset": "hipaa_age", "floor": 0, "under_label": "<0"})
        check_top_code_config(cfg)  # no raise


class TestFloorGeCap:
    def test_floor_equal_cap_rejected(self) -> None:
        cfg = _config({"cap": 89, "over_label": "90+", "floor": 89, "under_label": "<89"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_floor_ge_cap"
        assert exc.value.path == "tables.t.columns.age.provider_config.floor"

    def test_floor_greater_than_cap_rejected(self) -> None:
        cfg = _config({"cap": 89, "over_label": "90+", "floor": 100, "under_label": "<100"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_floor_ge_cap"

    def test_floor_less_than_cap_with_preset_compiles(self) -> None:
        cfg = _config({"preset": "hipaa_age", "floor": 0, "under_label": "<0"})
        check_top_code_config(cfg)  # no raise


class TestNonTopCodeColumnsIgnored:
    def test_non_top_code_column_ignored(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "age",
                            "strategy": "bucketize",
                            "provider_config": {},
                        }
                    ],
                }
            ]
        }
        check_top_code_config(cfg)  # no raise: not a top_code column

    def test_multiple_tables_checked_independently(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "good",
                    "columns": [
                        {
                            "name": "age",
                            "strategy": "top_code",
                            "provider_config": {"preset": "hipaa_age"},
                        }
                    ],
                },
                {
                    "name": "bad",
                    "columns": [
                        {
                            "name": "age",
                            "strategy": "top_code",
                            "provider_config": {},
                        }
                    ],
                },
            ]
        }
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.path == "tables.bad.columns.age.provider_config.cap"
