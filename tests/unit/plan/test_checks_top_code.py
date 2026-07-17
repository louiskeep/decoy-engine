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


class TestNonFiniteBoundsRejected:
    """Dennis HC-3b HIGH: a non-finite cap/floor (NaN/inf) is a float, so it
    slips past the isinstance check, but `value > nan`/`> inf` never fires --
    nothing is generalized and the column passes through unmasked. Reject it at
    compile (mirrors the handler's `math.isfinite` guard)."""

    @pytest.mark.parametrize("bad_cap", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_cap_rejected(self, bad_cap: float) -> None:
        cfg = _config({"cap": bad_cap, "over_label": "90+"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_bounds_unresolvable"
        assert exc.value.path == "tables.t.columns.age.provider_config.cap"

    @pytest.mark.parametrize("bad_floor", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_floor_rejected(self, bad_floor: float) -> None:
        cfg = _config({"cap": 89, "over_label": "90+", "floor": bad_floor, "under_label": "low"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_invalid_floor"
        assert exc.value.path == "tables.t.columns.age.provider_config.floor"

    def test_finite_bounds_still_pass(self) -> None:
        # Guard against over-rejection: ordinary finite bounds must be accepted.
        check_top_code_config(_config({"cap": 89, "over_label": "90+"}))
        check_top_code_config(
            _config({"cap": 100, "over_label": "hi", "floor": 0, "under_label": "lo"})
        )


class TestCodexCrossModelRejections:
    """Codex cross-model gate findings: shapes that previously slipped past
    compile (silent leak, raw crash, or ignored config). Each must now be a
    clean PlanCompileError."""

    def _nested(self, strategy_config: dict[str, Any]) -> dict[str, Any]:
        return {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "payload",
                            "strategy": "nested",
                            "namespace": "ns",
                            "provider_config": {
                                "strategy": "top_code",
                                "target": "$.ages[*]",
                                "strategy_config": strategy_config,
                            },
                        }
                    ],
                }
            ]
        }

    def test_nested_top_code_rejected(self) -> None:
        # BLOCKER 2/3: nested(top_code) mis-maps RowError to the wrong outer row
        # and bypasses bottom-bound checks. Reject outright.
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(self._nested({"cap": 89, "over_label": "OVER"}))
        assert exc.value.code == "top_code_unsupported_in_nested"

    def test_nested_top_code_with_malformed_floor_rejected(self) -> None:
        # The malformed-floor nested child (BLOCKER 3) is caught by the nested
        # guard before it can silently disable the floor.
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(
                self._nested({"cap": 89, "over_label": "O", "floor": "0", "under_label": "U"})
            )
        assert exc.value.code == "top_code_unsupported_in_nested"

    @pytest.mark.parametrize("bad_cap", [2**53, 2**53 + 1, 10**400, -(2**53)])
    def test_cap_at_or_beyond_2_to_53_rejected(self, bad_cap: int) -> None:
        # BLOCKER 1 + MEDIUM 2: past 2**53 float64 can't compare exactly, so a
        # tail value could escape generalization (leak). 10**400 also used to
        # raise OverflowError from math.isfinite; now a clean reject.
        cfg = _config({"cap": bad_cap, "over_label": "OVER"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_bounds_unresolvable"

    def test_floor_beyond_2_to_53_rejected(self) -> None:
        cfg = _config({"cap": 89, "over_label": "O", "floor": -(2**53), "under_label": "U"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_invalid_floor"

    def test_cap_just_below_2_to_53_still_passes(self) -> None:
        check_top_code_config(_config({"cap": 2**53 - 1, "over_label": "OVER"}))

    @pytest.mark.parametrize("bad_preset", [[], {}, 123])
    def test_unhashable_or_non_string_preset_rejected_cleanly(self, bad_preset: Any) -> None:
        # MEDIUM 2: `preset: []`/`{}` used to raise TypeError (unhashable) from
        # the membership test. Now a clean PlanCompileError.
        cfg = _config({"preset": bad_preset})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_bounds_unresolvable"

    def test_under_label_without_floor_rejected(self) -> None:
        # MEDIUM 3: an incomplete bottom-bound pair used to be silently ignored.
        cfg = _config({"cap": 89, "over_label": "OVER", "under_label": "UNDER"})
        with pytest.raises(PlanCompileError) as exc:
            check_top_code_config(cfg)
        assert exc.value.code == "top_code_under_label_without_floor"

    def test_nested_non_top_code_child_not_wrongly_rejected(self) -> None:
        # A nested child of a DIFFERENT strategy must not trip the top_code guard.
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "payload",
                            "strategy": "nested",
                            "namespace": "ns",
                            "provider_config": {
                                "strategy": "date_shift",
                                "target": "$.d[*]",
                                "strategy_config": {"min_days": -1, "max_days": 1},
                            },
                        }
                    ],
                }
            ]
        }
        check_top_code_config(cfg)  # no raise
