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
    detect_cvv,
    detect_email,
    detect_eu_date,
    detect_iban,
    detect_ipv4,
    detect_iso_date,
    detect_pan,
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


# ── pan (credit card) ────────────────────────────────────────────────────────

class TestPAN:
    # Real test card numbers from the major-issuer test ranges (all Luhn-valid).
    VALID = [
        "4111111111111111",   # Visa
        "5555555555554444",   # Mastercard
        "378282246310005",    # Amex (15 digits)
        "6011111111111117",   # Discover
    ]

    def test_clean_pan_column_fires(self):
        s = pd.Series(self.VALID)
        m = detect_pan(s, "card_number")
        assert m is not None and m.detector_id == "pan"
        assert m.match_rate == 1.0

    def test_grouped_format_with_dashes_fires(self):
        s = pd.Series(["4111-1111-1111-1111", "5555 5555 5555 4444"])
        m = detect_pan(s, "card")
        assert m is not None and m.match_rate == 1.0

    def test_random_digits_dont_fire(self):
        # 16 digits but Luhn-invalid — would false-positive if regex alone.
        s = pd.Series(["1234567890123456", "9999888877776666", "1111222233334444"])
        m = detect_pan(s, "transaction_id")
        assert m is None, "regex-only path would have fired; Luhn must reject"

    def test_short_digit_strings_dont_fire(self):
        # 12 digits — below the PAN minimum.
        s = pd.Series(["123456789012", "999999999999"])
        m = detect_pan(s, "ref")
        assert m is None


# ── cvv ──────────────────────────────────────────────────────────────────────

class TestCVV:
    def test_only_fires_with_name_hint(self):
        # Without the name hint CVV is uselessly broad — any 3-digit string.
        s = pd.Series(["123", "456", "789"])
        assert detect_cvv(s, "random_col") is None

    def test_fires_with_cvv_column_name(self):
        s = pd.Series(["123", "456", "789", "012"])
        m = detect_cvv(s, "cvv")
        assert m is not None and m.detector_id == "cvv"

    def test_fires_with_cvc_column_name(self):
        s = pd.Series(["123", "4567"])
        m = detect_cvv(s, "card_cvc")
        assert m is not None

    def test_security_code_alias(self):
        s = pd.Series(["123", "456"])
        m = detect_cvv(s, "card_security_code")
        assert m is not None


# ── iban ─────────────────────────────────────────────────────────────────────

class TestIBAN:
    # Real test IBANs from Wikipedia / SWIFT — all mod-97 valid.
    VALID = [
        "GB82WEST12345698765432",   # UK
        "DE89370400440532013000",   # Germany
        "FR1420041010050500013M02606",   # France
        "ES9121000418450200051332",   # Spain
    ]

    def test_clean_iban_column_fires(self):
        s = pd.Series(self.VALID)
        m = detect_iban(s, "iban")
        assert m is not None and m.detector_id == "iban"
        assert m.match_rate == 1.0

    def test_invalid_checksum_doesnt_fire(self):
        # Right shape, wrong checksum — mod-97 should reject.
        s = pd.Series([
            "GB00WEST12345698765432",
            "DE00370400440532013000",
        ])
        m = detect_iban(s, "iban")
        assert m is None, "regex-only path would have fired; mod-97 must reject"

    def test_random_alphanumerics_dont_fire(self):
        s = pd.Series(["ABCD1234567890XYZ", "XX99ABCDE12345678"])
        m = detect_iban(s, "code")
        assert m is None


# ── ipv4 ─────────────────────────────────────────────────────────────────────

class TestIPv4:
    def test_clean_ip_column_fires(self):
        s = pd.Series(["192.168.1.1", "10.0.0.5", "8.8.8.8", "203.0.113.7"])
        m = detect_ipv4(s, "client_ip")
        assert m is not None and m.detector_id == "ipv4"
        assert m.match_rate == 1.0

    def test_out_of_range_octet_doesnt_match(self):
        # Regex matches but validator rejects octet > 255.
        s = pd.Series(["999.1.1.1", "1.2.3.999", "256.256.256.256"])
        m = detect_ipv4(s, "ip")
        assert m is None

    def test_partial_match_doesnt_fire(self):
        s = pd.Series(["1.2.3", "1.2.3.4.5", "abc.def.ghi.jkl"])
        m = detect_ipv4(s, "ip")
        assert m is None


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
