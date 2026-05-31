"""MG-2 Step 1 (2026-05-31): iter_spans regression cells.

Locks the public contract of `storm.detectors.iter_spans`:
- Built-in detectors return spans aligned to original text offsets.
- Validators gate matches (invalid Luhn / IBAN / NPI / ICD-10 dropped).
- Overlap resolution is leftmost-then-longest.
- Subset / unknown detector_ids behave as documented.
- Custom detectors compose with the built-ins.
"""

from __future__ import annotations

import re

import pytest

from decoy_engine.storm.detectors import Span, iter_spans


# ── empty / non-string inputs ─────────────────────────────────────────


class TestEdgeCaseInputs:
    def test_iter_spans_empty_text_returns_empty_list(self):
        assert iter_spans("") == []

    def test_iter_spans_non_string_returns_empty_list(self):
        assert iter_spans(None) == []  # type: ignore[arg-type]
        assert iter_spans(42) == []  # type: ignore[arg-type]
        assert iter_spans(["nope"]) == []  # type: ignore[arg-type]

    def test_iter_spans_no_matches_returns_empty_list(self):
        assert iter_spans("Just some clinical prose with no PII.") == []


# ── single detector hits ──────────────────────────────────────────────


class TestSingleDetectorHits:
    def test_iter_spans_single_email_match(self):
        spans = iter_spans("Contact: alice@example.com please.")
        emails = [s for s in spans if s.detector_id == "email"]
        assert len(emails) == 1
        assert emails[0].matched_text == "alice@example.com"
        assert emails[0].start == 9
        assert emails[0].end == 26

    def test_iter_spans_single_ssn_match_with_dashes(self):
        spans = iter_spans("SSN 123-45-6789 on file.")
        ssns = [s for s in spans if s.detector_id == "ssn"]
        assert len(ssns) == 1
        assert ssns[0].matched_text == "123-45-6789"

    def test_iter_spans_single_ssn_match_without_dashes(self):
        spans = iter_spans("SSN 123456789 on file.")
        ssns = [s for s in spans if s.detector_id == "ssn"]
        assert len(ssns) == 1
        assert ssns[0].matched_text == "123456789"

    def test_iter_spans_single_us_phone_match(self):
        spans = iter_spans("Call (212) 555-1234 today.")
        phones = [s for s in spans if s.detector_id == "us_phone"]
        assert len(phones) == 1
        assert phones[0].matched_text == "(212) 555-1234"


# ── validator gating ──────────────────────────────────────────────────


class TestValidatorGating:
    def test_iter_spans_pan_validator_gates_match(self):
        spans = iter_spans("Card 1234 5678 9012 3456 invalid.", ["pan"])
        assert all(s.detector_id != "pan" for s in spans), (
            "non-Luhn PAN must not be returned as a span"
        )

    def test_iter_spans_pan_validator_passes_valid_luhn(self):
        spans = iter_spans("Card 4111 1111 1111 1111 valid.", ["pan"])
        pans = [s for s in spans if s.detector_id == "pan"]
        assert len(pans) == 1, "valid Luhn PAN must be returned as a span"

    def test_iter_spans_npi_validator_gates_match(self):
        spans = iter_spans("NPI 1234567890 noise.", ["npi"])
        assert all(s.detector_id != "npi" for s in spans)

    def test_iter_spans_npi_validator_passes_valid(self):
        # 1234567893 is a known-valid NPI per detectors.py docstring.
        spans = iter_spans("NPI 1234567893 valid.", ["npi"])
        npis = [s for s in spans if s.detector_id == "npi"]
        assert len(npis) == 1

    def test_iter_spans_iban_validator_gates_match(self):
        spans = iter_spans("IBAN AB99XXXXXXXXXXXXXXX noise.", ["iban"])
        assert all(s.detector_id != "iban" for s in spans)

    def test_iter_spans_icd10_validator_gates_match(self):
        # P97 is outside chapter P's valid range (P00-P96).
        spans = iter_spans("Code P97.0 noise.", ["icd10"])
        assert all(s.detector_id != "icd10" for s in spans)

    def test_iter_spans_icd10_validator_passes_valid(self):
        spans = iter_spans("Code J18.9 valid.", ["icd10"])
        icd = [s for s in spans if s.detector_id == "icd10"]
        assert len(icd) == 1
        assert icd[0].matched_text.upper().startswith("J18")


# ── multi-detector / ordering ─────────────────────────────────────────


class TestMultiDetector:
    def test_iter_spans_multi_detector_in_one_cell_returns_all_in_order(self):
        text = "Patient alice@example.com, SSN 123-45-6789, phone (212) 555-1234."
        spans = iter_spans(text)
        ids = [s.detector_id for s in spans]
        assert "email" in ids
        assert "ssn" in ids
        assert "us_phone" in ids
        for i in range(len(spans) - 1):
            assert spans[i].start <= spans[i + 1].start

    def test_iter_spans_overlap_resolution_leftmost_then_longest(self):
        text = "0123456789"  # 10 digits
        spec_short = {
            "detector_id": "short",
            "pattern": re.compile(r"\d{3}"),
            "validator": None,
        }
        spec_long = {
            "detector_id": "long",
            "pattern": re.compile(r"\d{6}"),
            "validator": None,
        }
        spans = iter_spans(text, detector_ids=[], custom=[spec_short, spec_long])
        # Both start at 0; the 6-digit match wins by length. After that
        # the remaining 4 digits at position 6 are too short to be
        # picked up by the long pattern, but the 3-digit at position 6
        # is non-overlapping with the kept (0,6).
        assert spans[0].detector_id == "long"
        assert spans[0].start == 0 and spans[0].end == 6
        assert spans[1].start >= 6


# ── detector_ids selection ────────────────────────────────────────────


class TestDetectorIdSelection:
    def test_iter_spans_subset_detector_ids_only_returns_listed(self):
        text = "Patient alice@example.com, SSN 123-45-6789."
        spans = iter_spans(text, ["email"])
        assert all(s.detector_id == "email" for s in spans)

    def test_iter_spans_unknown_detector_id_silently_skipped(self):
        text = "Patient alice@example.com."
        spans = iter_spans(text, ["nonexistent", "email"])
        ids = [s.detector_id for s in spans]
        assert ids == ["email"]


# ── custom detectors ──────────────────────────────────────────────────


class TestCustomDetectors:
    def test_iter_spans_custom_detector_returns_matches(self):
        spec = {
            "detector_id": "patient_id",
            "pattern": re.compile(r"PT-\d{6}"),
            "validator": None,
        }
        spans = iter_spans("Refer PT-123456 to clinic.", detector_ids=[], custom=[spec])
        assert len(spans) == 1
        assert spans[0].detector_id == "patient_id"
        assert spans[0].matched_text == "PT-123456"

    def test_iter_spans_custom_detector_validator_gates(self):
        # Validator rejects every match; pattern still matches.
        spec = {
            "detector_id": "noisy",
            "pattern": re.compile(r"\d{3}"),
            "validator": lambda v: False,
        }
        spans = iter_spans("123 456 789", detector_ids=[], custom=[spec])
        assert spans == []

    def test_iter_spans_custom_dedupes_against_builtin_overlap(self):
        # The custom pattern overlaps the SSN regex on a real SSN. The
        # leftmost-then-longest policy keeps whichever sort key wins; the
        # invariant under test is "no two returned spans overlap".
        spec = {
            "detector_id": "custom_id",
            "pattern": re.compile(r"\d{3}-\d{2}-\d{4}"),
            "validator": None,
        }
        spans = iter_spans("SSN 123-45-6789 here.", custom=[spec])
        last_end = -1
        for s in spans:
            assert s.start >= last_end, (
                f"span {s} overlaps previous (last_end={last_end})"
            )
            last_end = s.end


# ── Span dataclass shape ──────────────────────────────────────────────


class TestSpanShape:
    def test_span_is_frozen_dataclass(self):
        s = Span(detector_id="email", start=0, end=5, matched_text="abc@x")
        with pytest.raises(Exception):
            s.start = 1  # type: ignore[misc]

    def test_span_matched_text_round_trips_via_offsets(self):
        text = "x alice@example.com y"
        spans = iter_spans(text, ["email"])
        assert spans
        s = spans[0]
        assert text[s.start : s.end] == s.matched_text
