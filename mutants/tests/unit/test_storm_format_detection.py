"""Item 65 - STORM format-preservation detection tests.

Two layers under test:
  - Per-detector ``format_pattern`` on DetectorMatch (dominant sub-variant
    is recorded so the mask post-pass can splice separators back).
  - Profiler-level ``FieldStats.casing_pattern`` and
    ``FieldStats.format_pattern`` (the values consumed by the strategy
    post-pass).

Detectors without a format-variant table (email, person_name, etc.)
emit ``format_pattern=None`` - exercised in the cross-detector smoke
test below.
"""

import pandas as pd

from decoy_engine.storm.detectors import (
    detect_email,
    detect_eu_date,
    detect_icd10,
    detect_iso_date,
    detect_pan,
    detect_ssn,
    detect_us_date,
    detect_us_phone,
    detect_us_zip,
)
from decoy_engine.storm.profiler import (
    _detect_casing,
    _format_pattern_from_detectors,
    run_storm,
)
from decoy_engine.storm.types import DetectorMatch

# ── per-detector format variants ─────────────────────────────────────


class TestSSNFormatVariants:
    def test_dashed_variant_dominates(self):
        s = pd.Series(["123-45-6789", "555-12-3456", "111-22-3333"])
        m = detect_ssn(s, "ssn")
        assert m is not None
        assert m.format_pattern == r"\d{3}-\d{2}-\d{4}"

    def test_no_separator_variant_dominates(self):
        s = pd.Series(["123456789", "555123456", "111223333"])
        m = detect_ssn(s, "ssn")
        assert m is not None
        assert m.format_pattern == r"\d{9}"


class TestUSPhoneFormatVariants:
    def test_dashed_dominates(self):
        s = pd.Series(["815-233-3333", "212-555-0100", "415-867-5309"])
        m = detect_us_phone(s, "phone")
        assert m is not None
        assert m.format_pattern == r"\d{3}-\d{3}-\d{4}"

    def test_no_separator_dominates(self):
        s = pd.Series(["9123337893", "2125550100", "4158675309"])
        m = detect_us_phone(s, "phone")
        assert m is not None
        assert m.format_pattern == r"\d{10}"

    def test_paren_space_dash_dominates(self):
        s = pd.Series(["(815) 233-3333", "(212) 555-0100", "(415) 867-5309"])
        m = detect_us_phone(s, "phone")
        assert m is not None
        assert m.format_pattern == r"\(\d{3}\) \d{3}-\d{4}"


class TestUSZIPFormatVariants:
    def test_five_digit_dominates(self):
        s = pd.Series(["10001", "90210", "60601", "77001"])
        m = detect_us_zip(s, "zip")
        assert m is not None
        # S13-rebaseline P1 (2026-06-01): detector emits a word-boundary-
        # aware variant of the 5-digit ZIP pattern so the regex won't
        # match inside a longer numeric token (e.g. a SSN) or as the
        # leading digits of a ZIP+4. Lookbehind/lookahead block adjacent
        # word chars and adjacent ".N" decimal continuations.
        assert m.format_pattern == r"(?<!\w)\d{5}(?!\w)(?!\.\d)"

    def test_nine_digit_dominates(self):
        s = pd.Series(["10001-1234", "90210-5678", "60601-9999"])
        m = detect_us_zip(s, "zip")
        assert m is not None
        assert m.format_pattern == r"\d{5}-\d{4}"


class TestISODateFormatVariants:
    def test_yyyy_mm_dd_dominates(self):
        s = pd.Series(["2025-06-28", "2024-01-15", "2023-12-31"])
        m = detect_iso_date(s, "date")
        assert m is not None
        assert m.format_pattern == "%Y-%m-%d"

    def test_yyyymmdd_dominates(self):
        s = pd.Series(["20250628", "20240115", "20231231"])
        m = detect_iso_date(s, "date")
        assert m is not None
        assert m.format_pattern == "%Y%m%d"

    def test_with_time_component(self):
        s = pd.Series(
            [
                "2025-06-28T14:30:00Z",
                "2024-01-15T09:00:00Z",
                "2023-12-31T23:59:59Z",
            ]
        )
        m = detect_iso_date(s, "date")
        assert m is not None
        assert m.format_pattern == "%Y-%m-%dT%H:%M:%SZ"


class TestUSDateFormatVariants:
    def test_four_digit_year_dominates(self):
        s = pd.Series(["06/28/2025", "01/15/2024", "12/31/2023"])
        m = detect_us_date(s, "date")
        assert m is not None
        assert m.format_pattern == "%m/%d/%Y"

    def test_two_digit_year_dominates(self):
        s = pd.Series(["06/28/25", "01/15/24", "12/31/23"])
        m = detect_us_date(s, "date")
        assert m is not None
        assert m.format_pattern == "%m/%d/%y"


class TestEUDateFormatVariants:
    def test_dot_separator(self):
        s = pd.Series(["28.06.2025", "15.01.2024", "31.12.2023"])
        m = detect_eu_date(s, "date")
        assert m is not None
        assert m.format_pattern == "%d.%m.%Y"

    def test_dash_separator(self):
        s = pd.Series(["28-06-2025", "15-01-2024", "31-12-2023"])
        m = detect_eu_date(s, "date")
        assert m is not None
        assert m.format_pattern == "%d-%m-%Y"


class TestPANFormatVariants:
    def test_grouped_with_spaces(self):
        # Real Luhn-valid test card numbers spaced in groups of 4.
        s = pd.Series(
            [
                "4111 1111 1111 1111",  # Visa test
                "5500 0000 0000 0004",  # Mastercard test
                "6011 1111 1111 1117",  # Discover test
            ]
        )
        m = detect_pan(s, "card_number")
        assert m is not None
        assert m.format_pattern == r"\d{4} \d{4} \d{4} \d{4}"


class TestICD10FormatVariants:
    def test_dot_variant(self):
        s = pd.Series(["A01.0", "M79.3", "Z23.5", "F32.9"])
        m = detect_icd10(s, "diagnosis")
        assert m is not None
        assert m.format_pattern == r"[A-Z]\d{2}\.[A-Z0-9]{1,4}"


class TestNoVariantDetectors:
    """Detectors without a variant table emit format_pattern=None."""

    def test_email_has_no_format_pattern(self):
        s = pd.Series(["a@b.com", "c@d.org", "eve@x.io", "bob@test.net"])
        m = detect_email(s, "email")
        assert m is not None
        assert m.format_pattern is None


# ── profiler helpers ─────────────────────────────────────────────────


class TestDetectCasing:
    def test_all_upper(self):
        assert _detect_casing(pd.Series(["ERICA", "JOHN", "MARY"])) == "upper"

    def test_all_lower(self):
        assert _detect_casing(pd.Series(["alice", "bob", "carol"])) == "lower"

    def test_title_case(self):
        assert _detect_casing(pd.Series(["Alice", "Bob", "Carol"])) == "title"

    def test_middle_initial_title_still_counts(self):
        # str.istitle treats every alpha-token's first char as the relevant
        # one - 'Mary M Smith' is title-cased.
        assert _detect_casing(pd.Series(["Mary M Smith", "John Q Public"])) == "title"

    def test_digits_only(self):
        assert _detect_casing(pd.Series(["12345", "67890", "11111"])) == "digits_only"

    def test_mixed_falls_back(self):
        assert _detect_casing(pd.Series(["iPhone", "macOS", "iPad"])) == "mixed"

    def test_empty_returns_none(self):
        assert _detect_casing(pd.Series([], dtype=str)) is None
        assert _detect_casing(pd.Series([None, None])) is None


class TestFormatPatternFromDetectors:
    def test_picks_first_with_pattern(self):
        # Highest match_rate first (sort already applied by run_all_detectors).
        matches = [
            DetectorMatch(detector_id="email", match_rate=0.95, format_pattern=None),
            DetectorMatch(detector_id="ssn", match_rate=0.80, format_pattern=r"\d{9}"),
        ]
        # Email's None is skipped; SSN's variant wins.
        assert _format_pattern_from_detectors(matches) == r"\d{9}"

    def test_none_when_no_variants(self):
        matches = [
            DetectorMatch(detector_id="email", match_rate=0.95, format_pattern=None),
        ]
        assert _format_pattern_from_detectors(matches) is None

    def test_empty_list(self):
        assert _format_pattern_from_detectors([]) is None


# ── end-to-end: FieldStats carries the fields after run_storm ────────


class TestFieldStatsCarriesFormatHints:
    def test_ssn_column_gets_dashed_variant(self):
        df = pd.DataFrame(
            {
                "ssn": ["123-45-6789", "555-12-3456", "111-22-3333", "222-33-4444"],
                "name": ["Alice", "Bob", "Carol", "Dave"],
            }
        )
        profile = run_storm(df, "users.csv")
        ssn_field = next(f for f in profile.fields if f.name == "ssn")
        assert ssn_field.format_pattern == r"\d{3}-\d{2}-\d{4}"
        # Casing on pure-digit string column.
        assert ssn_field.casing_pattern == "digits_only"

    def test_iso_date_column_gets_strptime(self):
        df = pd.DataFrame(
            {
                "dob": ["1985-04-12", "1990-08-22", "1975-11-03", "2001-02-28"],
            }
        )
        profile = run_storm(df, "patients.csv")
        dob = next(f for f in profile.fields if f.name == "dob")
        assert dob.format_pattern == "%Y-%m-%d"

    def test_upper_case_name_column(self):
        df = pd.DataFrame(
            {
                "last_name": ["SMITH", "JONES", "WILLIAMS", "BROWN"],
            }
        )
        profile = run_storm(df, "people.csv")
        f = next(fs for fs in profile.fields if fs.name == "last_name")
        assert f.casing_pattern == "upper"

    def test_email_column_carries_no_format_pattern(self):
        df = pd.DataFrame(
            {
                "email": ["a@b.com", "c@d.org", "e@f.io", "g@h.co"],
            }
        )
        profile = run_storm(df, "contacts.csv")
        e = next(f for f in profile.fields if f.name == "email")
        assert e.format_pattern is None
