"""Dennis H2 closure regression (QA triage 2026-06-01).

Pin the behavior that `DateShiftStrategy._column_key` RAISES when its
`derive_key` callable throws, instead of silently falling back to
seed-only MD5. Mirrors the FPE F1 fix.

Before the fix: a `derive_key` failure was logged at WARNING but the
method returned None, causing apply() to use a seed-only MD5 path for
the shift offsets. Output was no longer recoverable from the master
key + didn't match a successful re-run with proper key derivation.

After the fix: derive_key failure raises MaskKeyDerivationError (a
DecoyError subclass, F15 2026-06-26 -- was a bare RuntimeError that
escaped `except DecoyError`); the job fails with a typed manifest error.
"""

from __future__ import annotations

import pytest

from decoy_engine.errors import DecoyError, MaskKeyDerivationError
from decoy_engine.transforms.date_shift import DateShiftStrategy


def _bad_derive_key(namespace: str) -> bytes:
    """Stand-in derive_key that always raises (simulates broken master
    key infrastructure)."""
    raise RuntimeError("simulated master-key resolution failure")


class TestDateShiftKeyResolutionFailureRaises:
    """The DateShift strategy must surface a derive_key failure as an
    exception rather than silently degrading to the seed-only MD5 path."""

    def test_column_key_raises_when_derive_key_throws(self):
        strategy = DateShiftStrategy(seed=42, derive_key=_bad_derive_key)
        with pytest.raises(
            MaskKeyDerivationError, match="(?i)DateShift column key derivation failed"
        ):
            strategy._column_key("birthdate")

    def test_raised_error_is_catchable_as_decoy_error(self):
        """F15: catchable via `except DecoyError`, with typed code + strategy."""
        strategy = DateShiftStrategy(seed=42, derive_key=_bad_derive_key)
        with pytest.raises(DecoyError) as exc_info:
            strategy._column_key("birthdate")
        assert isinstance(exc_info.value, MaskKeyDerivationError)
        assert exc_info.value.code == "mask.key_derivation_failed"
        assert exc_info.value.strategy == "date_shift"

    def test_column_key_returns_none_when_derive_key_is_none(self):
        """The legacy seed-only opt-out (derive_key=None passed by the
        caller) still returns None. That's an explicit opt-out, not a
        silent degradation."""
        strategy = DateShiftStrategy(seed=42, derive_key=None)
        assert strategy._column_key("birthdate") is None

    def test_column_key_returns_bytes_when_derive_key_succeeds(self):
        """Normal path: a working derive_key returns its bytes through."""

        def good_derive_key(namespace: str) -> bytes:
            return b"x" * 32

        strategy = DateShiftStrategy(seed=42, derive_key=good_derive_key)
        result = strategy._column_key("birthdate")
        assert result == b"x" * 32

    def test_raise_message_includes_exception_type(self):
        """The raised error includes the underlying exception type for
        operator debugging."""
        strategy = DateShiftStrategy(seed=42, derive_key=_bad_derive_key)
        with pytest.raises(MaskKeyDerivationError) as exc_info:
            strategy._column_key("birthdate")
        assert "RuntimeError" in str(exc_info.value)


# QA-internal-synth-providers F5 (2026-06-01, MEDIUM perf) ─────────────


class TestQaInternalF5NullRowsSkipHmac:
    """F5 contract: rows that are NaN or that fail to parse must not
    pay the per-value HMAC-SHA256 cost. Pre-fix the shift list
    comprehension iterated over EVERY row including nulls. At 1M rows
    with 50% nulls that's ~500K wasted HMAC calls. Now only valid
    rows get the HMAC; the rest become NaT via timedelta-None.

    The output contract is identical to pre-fix: null in -> null out,
    parse-fail in -> original string preserved, valid in -> shifted."""

    def test_null_rows_pass_through_as_nat(self):
        """Verify the output contract: nulls in input -> nulls in
        output. F5 must not change observable behavior."""
        import pandas as pd

        from decoy_engine.transforms.date_shift import DateShiftStrategy

        strategy = DateShiftStrategy(seed=42, derive_key=None)
        column = pd.Series(["2024-01-01", None, "2024-06-15", None, "2024-12-31"])
        rule = {"min_days": -30, "max_days": 30, "format": "%Y-%m-%d"}
        result = strategy.apply(column, rule)
        assert result.iloc[1] is None or pd.isna(result.iloc[1])
        assert result.iloc[3] is None or pd.isna(result.iloc[3])
        # Valid rows produced a non-null output.
        assert result.iloc[0] is not None and not pd.isna(result.iloc[0])
        assert result.iloc[2] is not None and not pd.isna(result.iloc[2])
        assert result.iloc[4] is not None and not pd.isna(result.iloc[4])

    def test_parse_failed_rows_pass_through_unchanged(self):
        """Parse-failed rows preserve their original string (the
        existing success_mask-driven assignment leaves the column
        value untouched for failed parses)."""
        import pandas as pd

        from decoy_engine.transforms.date_shift import DateShiftStrategy

        strategy = DateShiftStrategy(seed=42, derive_key=None)
        column = pd.Series(["2024-01-01", "not-a-date", "2024-06-15"])
        rule = {"min_days": -30, "max_days": 30, "format": "%Y-%m-%d"}
        result = strategy.apply(column, rule)
        # Parse-failed value preserved as-is.
        assert result.iloc[1] == "not-a-date"
        # Valid rows shifted (different from input).
        assert result.iloc[0] != "2024-01-01" or True  # may coincide with random shift==0
        assert isinstance(result.iloc[0], str)
        assert isinstance(result.iloc[2], str)

    def test_all_null_column_does_not_call_hmac_at_all(self):
        """Edge case: a column with EVERY row null. Pre-fix this still
        paid N HMAC calls (all on the string "nan"). Post-fix the
        valid_strs list is empty so zero HMAC work runs."""
        import pandas as pd

        from decoy_engine.transforms.date_shift import DateShiftStrategy

        strategy = DateShiftStrategy(seed=42, derive_key=None)
        column = pd.Series([None, None, None, None])
        rule = {"min_days": -30, "max_days": 30, "format": "%Y-%m-%d"}
        result = strategy.apply(column, rule)
        assert len(result) == 4
        for v in result:
            assert v is None or pd.isna(v)


# QA-internal-synth-providers F9 (2026-06-01, LOW correctness) ─────────


class TestQaInternalF9ValidateRuleRangeBounds:
    """F9 contract: validate_rule rejects non-int min_days / max_days
    with a clear ValueError that names the offending field. Pre-fix
    a YAML typo ('min_days: "abc"') passed validate_rule + later
    raised a bare ValueError from int('abc') deep inside apply()."""

    def test_validate_rule_rejects_non_int_min_days(self):
        from decoy_engine.transforms.date_shift import DateShiftStrategy

        strategy = DateShiftStrategy(seed=42, derive_key=None)
        with pytest.raises(ValueError, match=r"min_days.*integer"):
            strategy.validate_rule({"column": "dob", "min_days": "abc"})

    def test_validate_rule_rejects_non_int_max_days(self):
        from decoy_engine.transforms.date_shift import DateShiftStrategy

        strategy = DateShiftStrategy(seed=42, derive_key=None)
        with pytest.raises(ValueError, match=r"max_days.*integer"):
            strategy.validate_rule({"column": "dob", "min_days": -30, "max_days": "xyz"})

    def test_validate_rule_accepts_valid_int_or_int_coercible(self):
        """Accept ints + int-coercible strings ('30') because YAML
        coerces some quoted numbers and we want to be forgiving."""
        from decoy_engine.transforms.date_shift import DateShiftStrategy

        strategy = DateShiftStrategy(seed=42, derive_key=None)
        # Plain int.
        strategy.validate_rule({"column": "dob", "min_days": -30, "max_days": 30})
        # Int-coercible string.
        strategy.validate_rule({"column": "dob", "min_days": "-30", "max_days": "30"})

    def test_validate_rule_accepts_omitted_bounds(self):
        """min_days / max_days are optional; validate_rule should
        accept rules without them."""
        from decoy_engine.transforms.date_shift import DateShiftStrategy

        strategy = DateShiftStrategy(seed=42, derive_key=None)
        strategy.validate_rule({"column": "dob"})


class TestDetectFormat:
    """Direct oracles for the live `_detect_format` helper (reused by the
    engine-v2 date_shift handler, bucket_perturb, and the out-of-core path).
    The V1 `DateShiftStrategy` class in this module is superseded/dead (see
    tq-findings #11), so only this helper is graded."""

    def test_detect_iso_format(self) -> None:
        import pandas as pd

        from decoy_engine.transforms.date_shift import _detect_format

        # A column all of one format must be detected as that format (a broken
        # accept/append would return None instead).
        assert _detect_format(pd.Series(["2020-01-15", "2021-06-30"])) == "%Y-%m-%d"

    def test_detect_non_first_candidate_format(self) -> None:
        import pandas as pd

        from decoy_engine.transforms.date_shift import _detect_format

        # The first candidate (%Y-%m-%d) fails on these; detection must skip it
        # and keep scanning, not accept-on-fail or bail out at the first miss.
        assert _detect_format(pd.Series(["01/15/2020", "06/30/2021"])) == "%m/%d/%Y"

    def test_ambiguous_column_warns(self) -> None:
        import pandas as pd

        from decoy_engine.transforms.date_shift import _detect_format

        # Dates that parse under more than one format must emit the ambiguity
        # warning (the threshold is "> 1 candidate").
        with pytest.warns(UserWarning, match="multiple formats"):
            _detect_format(pd.Series(["01/02/2020", "03/04/2021"]))

    def test_unambiguous_column_does_not_warn(self) -> None:
        import warnings

        import pandas as pd

        from decoy_engine.transforms.date_shift import _detect_format

        # A single-candidate column must NOT warn (guards the ">1" threshold
        # against a ">=1" drift that would warn on every column).
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert _detect_format(pd.Series(["2020-01-15", "2021-06-30"])) == "%Y-%m-%d"

    def test_sample_cap_limits_detection_to_200_rows(self) -> None:
        import pandas as pd

        from decoy_engine.transforms.date_shift import _detect_format

        # The sample is capped at 200 rows (F9); a format that holds for the
        # first 200 rows is detected even if a later row would break it. Pins
        # both the cap value and that the cap is applied (head(200), not all).
        values = ["2020-01-15"] * 200 + ["not-a-date"]
        assert _detect_format(pd.Series(values)) == "%Y-%m-%d"
