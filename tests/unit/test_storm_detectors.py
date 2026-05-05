"""Per-detector unit tests for decoy_engine.storm.detectors.

Each detector gets at minimum:
  - a positive case (clean data → fires with high match_rate)
  - a negative case (unrelated data → does not fire)
  - a column-name hint case (low match rate but right name → still fires)

Plus a small registry-level test for the orchestrator.
"""

import pandas as pd
import pytest

from decoy_engine.storm.detectors import (
    REGISTERED_DETECTORS,
    detect_email,
    detect_eu_date,
    detect_iso_date,
    detect_person_name,
    detect_ssn,
    detect_us_date,
    detect_us_phone,
    detect_us_zip,
    run_all_detectors,
)


# ── email ────────────────────────────────────────────────────────────────────

class TestEmail:
    def test_clean_email_column_fires(self):
        s = pd.Series(["a@b.com", "c@d.org", "eve@x.io", "bob@test.net"])
        m = detect_email(s, "email")
        assert m is not None and m.detector_id == "email"
        assert m.match_rate == 1.0

    def test_unrelated_column_does_not_fire(self):
        s = pd.Series(["red", "blue", "green"])
        assert detect_email(s, "color") is None

    def test_partial_match_below_threshold_does_not_fire_without_hint(self):
        # 50% emails, 50% nonsense, column name unrelated → silent.
        s = pd.Series(["a@b.com", "c@d.com", "hello", "world"])
        assert detect_email(s, "freeform") is None

    def test_partial_match_with_name_hint_still_fires(self):
        # 50% emails, but column name says "email" → fires at lower threshold.
        s = pd.Series(["a@b.com", "c@d.com", "hello", "world"])
        m = detect_email(s, "email")
        assert m is not None and m.match_rate == 0.5


# ── ssn ──────────────────────────────────────────────────────────────────────

class TestSSN:
    def test_clean_ssn_column_fires(self):
        s = pd.Series(["123-45-6789", "555-12-3456", "111-22-3333", "444-55-6677"])
        m = detect_ssn(s, "ssn")
        assert m is not None and m.match_rate == 1.0

    def test_invalid_area_numbers_rejected(self):
        # SSA invalid area numbers: 000, 666, 9xx → these don't match the pattern
        s = pd.Series(["000-12-3456", "666-12-3456", "987-12-3456"])
        assert detect_ssn(s, "ssn_field") is None

    def test_dash_optional(self):
        s = pd.Series(["123456789", "555123456", "111223333"])
        m = detect_ssn(s, "ssn")
        assert m is not None and m.match_rate == 1.0

    def test_unrelated_data(self):
        s = pd.Series(["foo", "bar", "baz"])
        assert detect_ssn(s, "comment") is None


# ── us_phone ─────────────────────────────────────────────────────────────────

class TestUSPhone:
    def test_clean_phones_fire(self):
        s = pd.Series([
            "(555) 234-5678",
            "555.234.5678",
            "+1 555 234 5678",
            "5552345678",
        ])
        m = detect_us_phone(s, "phone")
        assert m is not None and m.match_rate >= 0.9

    def test_invalid_area_or_prefix_rejected(self):
        # NANP rules: area code and prefix start 2-9, not 0/1.
        s = pd.Series(["111-222-3333", "100-555-1234"])
        assert detect_us_phone(s, "tel") is None


# ── us_zip ───────────────────────────────────────────────────────────────────

class TestUSZip:
    def test_5_and_9_digit_zips_fire(self):
        s = pd.Series(["90210", "10001-1234", "60601", "94102-5678"])
        m = detect_us_zip(s, "zip")
        assert m is not None and m.match_rate == 1.0

    def test_unrelated_data(self):
        s = pd.Series(["abc", "def", "1234", "12"])
        assert detect_us_zip(s, "code") is None


# ── person_name ──────────────────────────────────────────────────────────────

class TestPersonName:
    def test_name_hinted_column_fires(self):
        s = pd.Series(["Alice Smith", "Bob Jones", "Carol O'Brien", "Dave"])
        m = detect_person_name(s, "first_name")
        assert m is not None

    def test_name_hinted_column_required(self):
        # Same data, column name doesn't hint → person_name stays silent.
        s = pd.Series(["Alice Smith", "Bob Jones", "Carol O'Brien", "Dave"])
        assert detect_person_name(s, "label") is None

    def test_full_name_column_matches(self):
        s = pd.Series(["Alice Smith", "Bob Jones"])
        assert detect_person_name(s, "full_name") is not None

    def test_bare_name_column_matches(self):
        s = pd.Series(["Alice Smith", "Bob Jones"])
        assert detect_person_name(s, "name") is not None


# ── date format detectors ────────────────────────────────────────────────────

class TestDateFormats:
    def test_iso_date(self):
        s = pd.Series(["2024-01-15", "2024-07-22", "1990-03-08"])
        m = detect_iso_date(s, "created_date")
        assert m is not None and m.match_rate == 1.0

    def test_us_date(self):
        s = pd.Series(["01/15/2024", "07/22/2024", "3/8/1990"])
        m = detect_us_date(s, "date")
        assert m is not None and m.match_rate == 1.0

    def test_eu_date(self):
        s = pd.Series(["15.01.2024", "22.07.2024", "8.3.1990"])
        m = detect_eu_date(s, "date")
        assert m is not None and m.match_rate == 1.0


# ── registry orchestrator ────────────────────────────────────────────────────

class TestRunAllDetectors:
    def test_returns_matches_sorted_by_descending_rate(self):
        # SSN-shaped + name-hinted as "ssn" → ssn detector fires first.
        s = pd.Series(["123-45-6789", "555-12-3456", "111-22-3333"])
        matches = run_all_detectors(s, "ssn")
        assert len(matches) >= 1
        assert matches[0].detector_id == "ssn"
        # Sort invariant: descending match_rate.
        for i in range(len(matches) - 1):
            assert matches[i].match_rate >= matches[i + 1].match_rate

    def test_no_matches_for_random_data(self):
        s = pd.Series(["random-stuff-1", "random-stuff-2", "lorem ipsum"])
        assert run_all_detectors(s, "freeform") == []

    def test_all_registered_detectors_callable(self):
        # Smoke test: every registered detector accepts the standard signature.
        s = pd.Series(["x", "y", "z"])
        for fn in REGISTERED_DETECTORS:
            result = fn(s, "any_col")
            assert result is None or hasattr(result, "detector_id")


# ── sample_misses behavior ───────────────────────────────────────────────────

def test_sample_misses_capped_at_3():
    # 5 emails + 5 misses → above name-hint threshold, but more than 3 misses.
    s = pd.Series([
        "a@b.com", "c@d.com", "e@f.com", "g@h.com", "i@j.com",
        "miss1", "miss2", "miss3", "miss4", "miss5",
    ])
    m = detect_email(s, "email")
    assert m is not None
    assert len(m.sample_misses) <= 3
