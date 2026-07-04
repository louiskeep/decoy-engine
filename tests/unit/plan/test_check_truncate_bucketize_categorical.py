"""Sprint 13 / coercion-13 S3: fail-closed compile checks for truncate and its
GATE-1 Q4 siblings (bucketize custom-width, categorical char-iteration).

Each strategy gets a TDD-shaped pair: a test proving the historical HANDLER
silently passed through / corrupted the column on the bad config (the leak),
and a test proving the new compile check now rejects the same config loudly
before a run ever starts. The handler-level proof lives in the execution
test files (test_truncate_keep_mask_char.py, test_hash_bucketize.py,
test_categorical_char_iteration.py); this file is the plan-compile half.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine.plan._checks_bucketize import check_bucketize_config
from decoy_engine.plan._checks_categorical import check_categorical_categories
from decoy_engine.plan._checks_truncate import check_truncate_config
from decoy_engine.plan._errors import PlanCompileError


def _config(strategy: str, provider_config: dict[str, Any], *, column: str = "c") -> dict[str, Any]:
    return {
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": column,
                        "strategy": strategy,
                        "provider": "p",
                        "provider_config": provider_config,
                    }
                ],
            }
        ]
    }


class TestCheckTruncateConfig:
    def test_string_length_rejected(self) -> None:
        """The exact shape a Studio picker's uncoerced numeric field
        produces (Sprint 13 finding 0.1/0.2): length arrives as a string."""
        cfg = _config("truncate", {"length": "3"})
        with pytest.raises(PlanCompileError) as exc:
            check_truncate_config(cfg)
        assert exc.value.code == "truncate_length_invalid"
        assert "t.c" in exc.value.path
        assert "3" in exc.value.message

    def test_missing_length_rejected(self) -> None:
        cfg = _config("truncate", {})
        with pytest.raises(PlanCompileError) as exc:
            check_truncate_config(cfg)
        assert exc.value.code == "truncate_length_invalid"

    def test_zero_length_rejected(self) -> None:
        cfg = _config("truncate", {"length": 0})
        with pytest.raises(PlanCompileError) as exc:
            check_truncate_config(cfg)
        assert exc.value.code == "truncate_length_invalid"

    def test_negative_length_rejected(self) -> None:
        cfg = _config("truncate", {"length": -1})
        with pytest.raises(PlanCompileError) as exc:
            check_truncate_config(cfg)
        assert exc.value.code == "truncate_length_invalid"

    def test_invalid_keep_rejected(self) -> None:
        cfg = _config("truncate", {"length": 3, "keep": "middle"})
        with pytest.raises(PlanCompileError) as exc:
            check_truncate_config(cfg)
        assert exc.value.code == "truncate_keep_invalid"

    def test_multi_char_mask_char_rejected(self) -> None:
        cfg = _config("truncate", {"length": 3, "mask_char": "XY"})
        with pytest.raises(PlanCompileError) as exc:
            check_truncate_config(cfg)
        assert exc.value.code == "truncate_mask_char_invalid"

    def test_non_string_mask_char_rejected(self) -> None:
        cfg = _config("truncate", {"length": 3, "mask_char": 42})
        with pytest.raises(PlanCompileError) as exc:
            check_truncate_config(cfg)
        assert exc.value.code == "truncate_mask_char_invalid"

    def test_valid_length_only_compiles(self) -> None:
        check_truncate_config(_config("truncate", {"length": 3}))  # no raise

    def test_valid_keep_and_mask_char_compile(self) -> None:
        check_truncate_config(
            _config("truncate", {"length": 4, "keep": "tail", "mask_char": "*"})
        )  # no raise

    def test_other_strategies_untouched(self) -> None:
        check_truncate_config(_config("hash", {"truncate": "not-an-int"}))  # no raise


class TestCheckBucketizeConfig:
    def test_custom_sentinel_with_string_width_rejected(self) -> None:
        """Sprint 13 D4: the Studio picker's '(custom)' sentinel reaching
        the engine alongside an uncoerced string width."""
        cfg = _config("bucketize", {"preset": "(custom)", "width": "10"})
        with pytest.raises(PlanCompileError) as exc:
            check_bucketize_config(cfg)
        assert exc.value.code == "bucketize_width_unresolvable"

    def test_unknown_preset_rejected(self) -> None:
        cfg = _config("bucketize", {"preset": "by_fortnight"})
        with pytest.raises(PlanCompileError) as exc:
            check_bucketize_config(cfg)
        assert exc.value.code == "bucketize_width_unresolvable"

    def test_string_width_rejected(self) -> None:
        cfg = _config("bucketize", {"width": "10"})
        with pytest.raises(PlanCompileError) as exc:
            check_bucketize_config(cfg)
        assert exc.value.code == "bucketize_width_unresolvable"

    def test_zero_width_rejected(self) -> None:
        cfg = _config("bucketize", {"width": 0})
        with pytest.raises(PlanCompileError) as exc:
            check_bucketize_config(cfg)
        assert exc.value.code == "bucketize_width_unresolvable"

    def test_missing_width_and_preset_rejected(self) -> None:
        cfg = _config("bucketize", {})
        with pytest.raises(PlanCompileError) as exc:
            check_bucketize_config(cfg)
        assert exc.value.code == "bucketize_width_unresolvable"

    def test_bool_width_rejected(self) -> None:
        cfg = _config("bucketize", {"width": True})
        with pytest.raises(PlanCompileError) as exc:
            check_bucketize_config(cfg)
        assert exc.value.code == "bucketize_width_unresolvable"

    def test_known_preset_compiles(self) -> None:
        check_bucketize_config(_config("bucketize", {"preset": "by_decade"}))  # no raise

    def test_valid_numeric_width_compiles(self) -> None:
        check_bucketize_config(_config("bucketize", {"width": 10}))  # no raise

    def test_valid_float_width_compiles(self) -> None:
        check_bucketize_config(_config("bucketize", {"width": 2.5}))  # no raise


class TestCheckCategoricalCategories:
    def test_string_categories_rejected(self) -> None:
        """Sprint 13 D5: a free-text Studio field submits categories as a
        bare string; list("gold,silver") iterates characters at runtime."""
        cfg = _config("categorical", {"categories": "gold,silver,bronze"})
        with pytest.raises(PlanCompileError) as exc:
            check_categorical_categories(cfg)
        assert exc.value.code == "categorical_categories_not_list"

    def test_missing_categories_rejected(self) -> None:
        cfg = _config("categorical", {})
        with pytest.raises(PlanCompileError) as exc:
            check_categorical_categories(cfg)
        assert exc.value.code == "categorical_categories_missing"

    def test_empty_list_categories_rejected(self) -> None:
        cfg = _config("categorical", {"categories": []})
        with pytest.raises(PlanCompileError) as exc:
            check_categorical_categories(cfg)
        assert exc.value.code == "categorical_categories_missing"

    def test_from_profile_exempts_missing_categories(self) -> None:
        check_categorical_categories(_config("categorical", {"from_profile": True}))  # no raise

    def test_from_profile_exempts_string_categories(self) -> None:
        """Defensive: from_profile short-circuits before the shape check."""
        check_categorical_categories(
            _config("categorical", {"from_profile": True, "categories": "whatever"})
        )  # no raise

    def test_valid_list_categories_compiles(self) -> None:
        check_categorical_categories(
            _config("categorical", {"categories": ["gold", "silver", "bronze"]})
        )  # no raise

    def test_valid_tuple_categories_compiles(self) -> None:
        check_categorical_categories(
            _config("categorical", {"categories": ("gold", "silver")})
        )  # no raise
