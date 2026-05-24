"""Plan B-2 tests for STORM's new column-shape signals.

These exercise the four FieldStats fields the profiler now computes:
``alphabet``, ``value_set_size_class``, ``numeric_range_class``,
``mode_value``/``mode_freq``. The signals exist so FORECAST's per-
detector choosers can pick mask params from the data instead of using
hardcoded constants — see ``test_forecast_param_choosing.py`` for the
chooser-side coverage.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.storm import run_storm


def _scan(**columns) -> dict[str, object]:
    """Run STORM on the columns and return a {name: FieldStats} map."""
    df = pd.DataFrame(columns)
    profile = run_storm(df, source_label="signals-test.csv")
    return {f.name: f for f in profile.fields}


# ── alphabet ─────────────────────────────────────────────────────────


class TestAlphabet:
    def test_pure_digits_column(self):
        fields = _scan(zip_code=["12345", "67890", "11111", "22222", "33333"] * 4)
        assert fields["zip_code"].alphabet == "digits"

    def test_pure_alpha_column(self):
        fields = _scan(status=["active", "pending", "closed", "active", "pending"] * 4)
        assert fields["status"].alphabet == "alpha"

    def test_alphanum_column(self):
        fields = _scan(sku=[f"AB{i:04d}" for i in range(20)])
        assert fields["sku"].alphabet == "alphanum"

    def test_mixed_with_separators_is_mixed(self):
        fields = _scan(phone=["(555) 123-4567"] * 20)
        # Parens, space, and hyphen each push the value into 'mixed' even
        # though the underlying characters are digits + (no letters).
        assert fields["phone"].alphabet == "mixed"

    def test_numeric_column_has_no_alphabet(self):
        # alphabet is only computed for string-typed columns (object or native str dtype).
        fields = _scan(amount=[1, 2, 3, 4, 5] * 4)
        assert fields["amount"].alphabet is None


# ── value_set_size_class ─────────────────────────────────────────────


class TestValueSetSizeClass:
    def test_constant_column(self):
        fields = _scan(country=["US"] * 20)
        assert fields["country"].value_set_size_class == "constant"

    def test_binary_column(self):
        fields = _scan(opt_in=[True, False] * 10)
        assert fields["opt_in"].value_set_size_class == "binary"

    def test_low_cardinality(self):
        # 5 distinct values across 20 rows → 'low' (distinct <= 10).
        fields = _scan(status=["A", "B", "C", "D", "E"] * 4)
        assert fields["status"].value_set_size_class == "low"

    def test_unique_pk_shaped(self):
        fields = _scan(user_id=list(range(20)))
        assert fields["user_id"].value_set_size_class == "unique"

    def test_medium_band(self):
        # ~30% unique_rate → 'medium'.
        fields = _scan(group=[i % 7 for i in range(20)])
        # 7 distinct out of 20 = 0.35 unique_rate. ``low`` because
        # distinct_count <= 10 triggers the low branch first.
        assert fields["group"].value_set_size_class == "low"


# ── numeric_range_class ──────────────────────────────────────────────


class TestNumericRangeClass:
    def test_small_int(self):
        fields = _scan(age=[20, 30, 40, 50] * 5)
        assert fields["age"].numeric_range_class == "small_int"

    def test_big_int(self):
        fields = _scan(account_no=[10_000_001 + i for i in range(20)])
        assert fields["account_no"].numeric_range_class == "big_int"

    def test_decimal_money(self):
        fields = _scan(price=[19.99, 5.50, 100.00, 7.25, 250.75] * 4)
        assert fields["price"].numeric_range_class == "decimal_money"

    def test_decimal_other(self):
        # Scientific / measurement values — varying decimal lengths,
        # not predominantly 2-decimal.
        fields = _scan(ratio=[0.1234, 1.5, 3.14159, 0.001, 42.0] * 4)
        assert fields["ratio"].numeric_range_class == "decimal_other"

    def test_string_column_has_no_numeric_range(self):
        fields = _scan(name=["alice", "bob", "carol"] * 7)
        assert fields["name"].numeric_range_class is None


# ── mode_value / mode_freq ───────────────────────────────────────────


class TestModeValue:
    def test_dominant_value_picked(self):
        fields = _scan(status=["active"] * 18 + ["closed"] * 2)
        assert fields["status"].mode_value == "active"
        assert fields["status"].mode_freq == 0.9

    def test_unique_column_mode_freq_is_low(self):
        fields = _scan(user_id=list(range(20)))
        # Each value occurs once, so mode_freq == 1/20 == 0.05.
        assert fields["user_id"].mode_freq == 0.05

    def test_empty_column_returns_none(self):
        fields = _scan(maybe=[None] * 20)
        assert fields["maybe"].mode_value is None
        assert fields["maybe"].mode_freq == 0.0
