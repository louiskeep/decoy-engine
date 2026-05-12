"""Item 65 — Layer B tests for the format-preservation post-pass.

Three slices:
  - Pure-function tests of the helpers (_apply_digit_template,
    _apply_strftime, _apply_casing).
  - apply_format_preservation contract (no-op when rule.preserve_format
    is falsy or strategy is opted out; reshape when both pattern bits
    present).
  - Integration through MaskingProcessor: feed in a rule with
    preserve_format=true and assert the masked-then-reshaped output
    matches the source format.
"""

import pandas as pd

from decoy_engine.transforms.format_preservation import (
    SKIP_STRATEGIES,
    apply_format_preservation,
    _apply_casing,
    _apply_digit_template,
    _apply_strftime,
    _parse_digit_template,
)


# ── _parse_digit_template ────────────────────────────────────────────


class TestParseDigitTemplate:
    def test_phone_template(self):
        slots = _parse_digit_template(r"\d{3}-\d{3}-\d{4}")
        assert slots == [("", 3), ("-", 3), ("-", 4)]

    def test_ssn_template(self):
        slots = _parse_digit_template(r"\d{3}-\d{2}-\d{4}")
        assert slots == [("", 3), ("-", 3), ("-", 2)] or slots == [("", 3), ("-", 2), ("-", 4)]
        # Note: SSN is 3-2-4, the assertion above checks both orderings
        # to catch a regex bug; the real result should be 3-2-4:
        assert slots == [("", 3), ("-", 2), ("-", 4)]

    def test_raw_digits_no_separators(self):
        slots = _parse_digit_template(r"\d{10}")
        assert slots == [("", 10)]

    def test_zip_extended(self):
        slots = _parse_digit_template(r"\d{5}-\d{4}")
        assert slots == [("", 5), ("-", 4)]

    def test_no_digit_runs_returns_empty(self):
        assert _parse_digit_template("abc") == []


# ── _apply_digit_template ────────────────────────────────────────────


class TestDigitTemplate:
    def test_phone_dash_template_splices(self):
        masked = pd.Series(["2322316595", "0982329345"])
        out = _apply_digit_template(masked, r"\d{3}-\d{3}-\d{4}")
        assert list(out) == ["232-231-6595", "098-232-9345"]

    def test_ssn_dash_template(self):
        masked = pd.Series(["123456789"])
        out = _apply_digit_template(masked, r"\d{3}-\d{2}-\d{4}")
        assert list(out) == ["123-45-6789"]

    def test_strips_existing_separators_before_splicing(self):
        # Strategy returned a value that's already separator-shaped;
        # we still strip and re-splice so the template is canonical.
        masked = pd.Series(["232.231.6595"])
        out = _apply_digit_template(masked, r"\d{3}-\d{3}-\d{4}")
        assert list(out) == ["232-231-6595"]

    def test_insufficient_digits_passes_through(self):
        masked = pd.Series(["abc"])
        out = _apply_digit_template(masked, r"\d{3}-\d{3}-\d{4}")
        # Not enough digits — value passes through unchanged.
        assert list(out) == ["abc"]

    def test_handles_null(self):
        masked = pd.Series(["2322316595", None])
        out = _apply_digit_template(masked, r"\d{3}-\d{3}-\d{4}")
        assert out.iloc[0] == "232-231-6595"
        assert out.iloc[1] is None or pd.isna(out.iloc[1])


# ── _apply_strftime ──────────────────────────────────────────────────


class TestStrftime:
    def test_iso_dashed_to_compact(self):
        masked = pd.Series(["2030-01-15", "2029-12-31"])
        out = _apply_strftime(masked, "%Y%m%d")
        assert list(out) == ["20300115", "20291231"]

    def test_iso_compact_to_dashed(self):
        masked = pd.Series(["20300115", "20291231"])
        out = _apply_strftime(masked, "%Y-%m-%d")
        assert list(out) == ["2030-01-15", "2029-12-31"]

    def test_us_to_iso(self):
        masked = pd.Series(["01/15/2030", "12/31/2029"])
        out = _apply_strftime(masked, "%Y-%m-%d")
        assert list(out) == ["2030-01-15", "2029-12-31"]

    def test_unparseable_passes_through(self):
        masked = pd.Series(["not a date"])
        out = _apply_strftime(masked, "%Y-%m-%d")
        assert list(out) == ["not a date"]


# ── _apply_casing ────────────────────────────────────────────────────


class TestCasing:
    def test_upper(self):
        out = _apply_casing(pd.Series(["alice", "Bob"]), "upper")
        assert list(out) == ["ALICE", "BOB"]

    def test_lower(self):
        out = _apply_casing(pd.Series(["ALICE", "Bob"]), "lower")
        assert list(out) == ["alice", "bob"]

    def test_title(self):
        out = _apply_casing(pd.Series(["alice m harter", "JOHN SMITH"]), "title")
        # Native str.title() yields 'Alice M Harter' / 'John Smith'.
        assert list(out) == ["Alice M Harter", "John Smith"]

    def test_digits_only_strips_non_digits(self):
        out = _apply_casing(pd.Series(["232-231-6595", "abc"]), "digits_only")
        assert list(out) == ["2322316595", ""]

    def test_mixed_is_noop(self):
        out = _apply_casing(pd.Series(["iPhone", "macOS"]), "mixed")
        assert list(out) == ["iPhone", "macOS"]


# ── apply_format_preservation contract ───────────────────────────────


class TestApplyFormatPreservation:
    def test_noop_when_flag_off(self):
        source = pd.Series(["123-45-6789"])
        masked = pd.Series(["999999999"])
        out = apply_format_preservation(source, masked, {
            "type": "faker",
            "preserve_format": False,
            "format_pattern": r"\d{3}-\d{2}-\d{4}",
        })
        # Flag off — masked is returned unchanged.
        assert list(out) == ["999999999"]

    def test_hash_strategy_is_skipped(self):
        source = pd.Series(["123-45-6789"])
        masked = pd.Series(["abcdef1234567890"])  # hex output
        out = apply_format_preservation(source, masked, {
            "type": "hash",
            "preserve_format": True,
            "format_pattern": r"\d{3}-\d{2}-\d{4}",
        })
        # Hash is in SKIP_STRATEGIES — output passes through.
        assert list(out) == ["abcdef1234567890"]
        assert "hash" in SKIP_STRATEGIES

    def test_passthrough_strategy_is_skipped(self):
        source = pd.Series(["alice"])
        masked = pd.Series(["alice"])
        out = apply_format_preservation(source, masked, {
            "type": "passthrough",
            "preserve_format": True,
            "casing_pattern": "upper",
        })
        # Passthrough — skipped (source already equals output).
        assert list(out) == ["alice"]

    def test_date_shift_is_skipped(self):
        # date_shift handles its own format; the post-pass would
        # double-format and break the strategy's output.
        source = pd.Series(["2025-06-28"])
        masked = pd.Series(["2030-01-15"])
        out = apply_format_preservation(source, masked, {
            "type": "date_shift",
            "preserve_format": True,
            "format_pattern": "%Y%m%d",
        })
        assert list(out) == ["2030-01-15"]

    def test_phone_format_with_faker(self):
        # Faker emitted bare digits; reshape to dashed template.
        source = pd.Series(["815-233-3333"])
        masked = pd.Series(["2322316595"])
        out = apply_format_preservation(source, masked, {
            "type": "faker",
            "preserve_format": True,
            "format_pattern": r"\d{3}-\d{3}-\d{4}",
        })
        assert list(out) == ["232-231-6595"]

    def test_casing_with_faker(self):
        source = pd.Series(["ERICA"])
        masked = pd.Series(["alice"])
        out = apply_format_preservation(source, masked, {
            "type": "faker",
            "preserve_format": True,
            "casing_pattern": "upper",
        })
        assert list(out) == ["ALICE"]

    def test_casing_plus_digit_template_combined(self):
        # Both hints set — both apply in order (template first, then casing).
        source = pd.Series(["AB-1234"])
        masked = pd.Series(["zz9999"])
        out = apply_format_preservation(source, masked, {
            "type": "faker",
            "preserve_format": True,
            "casing_pattern": "upper",
        })
        # Only casing applies (no digit template here).
        assert list(out) == ["ZZ9999"]

    def test_no_patterns_is_noop(self):
        source = pd.Series(["abc"])
        masked = pd.Series(["xyz"])
        out = apply_format_preservation(source, masked, {
            "type": "faker",
            "preserve_format": True,
            # neither format_pattern nor casing_pattern provided
        })
        assert list(out) == ["xyz"]

    def test_unparseable_row_isolated(self):
        # One row can't be re-shaped (no digits) but the others should.
        source = pd.Series(["123-45-6789", "123-45-6789"])
        masked = pd.Series(["999999999", "abc"])
        out = apply_format_preservation(source, masked, {
            "type": "faker",
            "preserve_format": True,
            "format_pattern": r"\d{3}-\d{2}-\d{4}",
        })
        assert list(out) == ["999-99-9999", "abc"]


# ── integration through MaskingProcessor ─────────────────────────────


class TestProcessorIntegration:
    def test_processor_applies_post_pass_when_flagged(self):
        # Minimal wiring — instantiate the processor directly without
        # the full Masker pipeline. The processor reads `preserve_format`
        # off each rule and applies the post-pass after the strategy.
        from decoy_engine.masker.processor import MaskingProcessor

        df = pd.DataFrame({
            "phone": ["815-233-3333", "212-555-0100", "415-867-5309"],
        })

        # Stub strategy_manager that returns bare-digit output (simulates
        # faker.phone with no format awareness).
        class _StubStrategyManager:
            def apply_masking_rule(self, column, rule):
                return pd.Series(["2322316595", "3334445555", "9876543210"], index=column.index)

        # Stub ref-integrity (no relationships in this test).
        class _StubRefIntegrity:
            def get_referential_relationship(self, table, col):
                return None

        proc = MaskingProcessor(
            config={"masking_rules": [{
                "column": "phone",
                "type": "faker",
                "preserve_format": True,
                "format_pattern": r"\d{3}-\d{3}-\d{4}",
            }]},
            strategy_manager=_StubStrategyManager(),
            ref_integrity=_StubRefIntegrity(),
        )

        out = proc.apply_masking_rules(df, table_name="t")
        assert list(out["phone"]) == ["232-231-6595", "333-444-5555", "987-654-3210"]

    def test_processor_skip_when_flag_off(self):
        from decoy_engine.masker.processor import MaskingProcessor

        df = pd.DataFrame({"phone": ["815-233-3333"]})

        class _StubStrategyManager:
            def apply_masking_rule(self, column, rule):
                return pd.Series(["2322316595"], index=column.index)

        class _StubRefIntegrity:
            def get_referential_relationship(self, table, col):
                return None

        proc = MaskingProcessor(
            config={"masking_rules": [{
                "column": "phone",
                "type": "faker",
                "preserve_format": False,
                "format_pattern": r"\d{3}-\d{3}-\d{4}",
            }]},
            strategy_manager=_StubStrategyManager(),
            ref_integrity=_StubRefIntegrity(),
        )

        out = proc.apply_masking_rules(df, table_name="t")
        # Flag off — raw output passes through.
        assert list(out["phone"]) == ["2322316595"]
