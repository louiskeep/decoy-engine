"""Tests for the clinical detectors added in Item 31 phase 3.

Covers icd10, npi, mrn (the three new format-validated / name-hint clinical
detectors) plus smoke tests for the seven HIPAA Safe Harbor completers
(url, fax_number, health_plan_id, license_num, vehicle_id, device_id,
biometric_id) that fire on name hint alone.
"""

import pandas as pd

from decoy_engine.storm.detectors import (
    _icd10_valid,
    _npi_valid,
    detect_biometric_id,
    detect_device_id,
    detect_fax_number,
    detect_health_plan_id,
    detect_icd10,
    detect_license_num,
    detect_mrn,
    detect_npi,
    detect_url,
    detect_vehicle_id,
)

# ── ICD-10 ──────────────────────────────────────────────────────────────────────────────


class TestICD10Detector:
    _VALID_CODES = ["A01.0", "M79.3", "S72.001A", "Z23", "F32.9"]

    def test_fires_on_valid_codes_with_name_hint(self):
        series = pd.Series(self._VALID_CODES * 20)
        result = detect_icd10(series, "diagnosis_code")
        assert result is not None
        assert result.detector_id == "icd10"
        assert result.match_rate == 1.0

    def test_fires_on_icd_code_col_name(self):
        series = pd.Series(self._VALID_CODES * 20)
        assert detect_icd10(series, "icd_code") is not None

    def test_fires_on_value_pattern_without_name_hint(self):
        # 100 valid codes, 0 invalids -> rate 1.0 ≥ DEFAULT_MIN_MATCH_RATE 0.7
        series = pd.Series(self._VALID_CODES * 20)
        result = detect_icd10(series, "arbitrary_col")
        assert result is not None

    def test_rejects_non_icd10_data(self):
        series = pd.Series(["12.345", "hello world", "XYZ!", "999.00"] * 25)
        assert detect_icd10(series, "random_col") is None

    def test_icd10_valid_passes_known_codes(self):
        for code in ["A010", "A01.0", "M793", "M79.3", "S72001A", "S72.001A", "Z23", "F329"]:
            assert _icd10_valid(code), f"Expected valid: {code!r}"

    def test_icd10_valid_rejects_bad_structure(self):
        for code in ["123.45", "AB.1", "1A2", "XY", "", "TOOLONGCODE123"]:
            assert not _icd10_valid(code), f"Expected invalid: {code!r}"


# ── NPI ─────────────────────────────────────────────────────────────────────────────────


class TestNPIDetector:
    # Known-good NPIs verified against the CMS Luhn algorithm:
    # 1234567893: prefix 80840123456789 -> sum 67 -> check 3 ✓
    # 1679576722: prefix 80840167957672 -> sum 68 -> check 2 ✓
    # 1000000004: prefix 80840100000000 -> sum 26 -> check 4 ✓
    _VALID_NPIS = ["1234567893", "1679576722", "1000000004"]

    def test_fires_on_valid_npis_with_name_hint(self):
        series = pd.Series(self._VALID_NPIS * 25)
        result = detect_npi(series, "npi")
        assert result is not None
        assert result.detector_id == "npi"
        assert result.match_rate == 1.0

    def test_fires_on_provider_npi_col(self):
        series = pd.Series(self._VALID_NPIS * 25)
        assert detect_npi(series, "provider_npi") is not None

    def test_rejects_wrong_check_digit(self):
        # 1234567893 is valid; 1234567890 has check digit 0, should be 3.
        series = pd.Series(["1234567890"] * 50)
        assert detect_npi(series, "random_col") is None

    def test_rejects_non_digit_values(self):
        series = pd.Series(["abc1234567", "npi-12345"] * 30)
        assert detect_npi(series, "npi") is None

    def test_npi_valid_accepts_known_npis(self):
        for npi in self._VALID_NPIS:
            assert _npi_valid(npi), f"Expected valid NPI: {npi!r}"

    def test_npi_valid_rejects_bad_length(self):
        assert not _npi_valid("123456789")  # 9 digits
        assert not _npi_valid("12345678901")  # 11 digits

    def test_npi_valid_rejects_wrong_check_digit(self):
        # Correct check for 123456789x is 3; anything else is invalid.
        assert not _npi_valid("1234567890")
        assert not _npi_valid("1234567891")
        assert not _npi_valid("1234567892")

    def test_npi_valid_rejects_non_digits(self):
        assert not _npi_valid("12345678AB")
        assert not _npi_valid("NPI-123456")


# ── MRN ─────────────────────────────────────────────────────────────────────────────────


class TestMRNDetector:
    def test_fires_on_mrn_col_name(self):
        series = pd.Series(["MRN12345", "MRN67890", "1234567", "PT98765"] * 20)
        result = detect_mrn(series, "mrn")
        assert result is not None
        assert result.detector_id == "mrn"

    def test_fires_on_medical_record_col(self):
        series = pd.Series(["MRN12345", "1234567"] * 40)
        assert detect_mrn(series, "medical_record_num") is not None

    def test_fires_on_patient_id_col(self):
        series = pd.Series(["PT001234", "PT005678"] * 40)
        assert detect_mrn(series, "patient_id") is not None

    def test_does_not_fire_without_name_hint(self):
        # MRN has no distinctive value format - name hint is mandatory.
        series = pd.Series(["MRN12345", "PT1234567"] * 50)
        assert detect_mrn(series, "some_id_col") is None

    def test_does_not_fire_on_too_short_values(self):
        # Values shorter than 4 chars don't satisfy the MRN regex minimum.
        series = pd.Series(["AB", "12", "X"] * 40)
        assert detect_mrn(series, "mrn") is None


# ── HIPAA Safe Harbor completers ───────────────────────────────────────────────────────


class TestHIPAASafeHarborCompletors:
    def test_url_fires_on_http_values_with_hint(self):
        series = pd.Series(
            [
                "https://example.com/patient/123",
                "https://hospital.org/records",
            ]
            * 40
        )
        result = detect_url(series, "profile_url")
        assert result is not None
        assert result.detector_id == "url"

    def test_url_fires_without_name_hint(self):
        # URL regex is distinctive enough to fire without a name hint.
        series = pd.Series(["https://example.com/path?id=1"] * 80)
        assert detect_url(series, "arbitrary_col") is not None

    def test_fax_fires_with_name_hint(self):
        series = pd.Series(["(555) 123-4567", "212-555-0100"] * 40)
        assert detect_fax_number(series, "fax_number") is not None

    def test_fax_does_not_fire_without_name_hint(self):
        # Same phone-format values; no fax hint -> silent.
        series = pd.Series(["(555) 123-4567", "212-555-0100"] * 40)
        assert detect_fax_number(series, "phone_col") is None

    def test_health_plan_id_fires_on_member_id_col(self):
        series = pd.Series(["BCBS123456", "UHC98765"] * 40)
        assert detect_health_plan_id(series, "member_id") is not None

    def test_health_plan_id_silent_without_hint(self):
        series = pd.Series(["BCBS123456", "UHC98765"] * 40)
        assert detect_health_plan_id(series, "some_col") is None

    def test_license_num_fires_on_license_num_col(self):
        series = pd.Series(["LIC20240001", "CERT12345"] * 40)
        assert detect_license_num(series, "license_num") is not None

    def test_license_num_silent_without_hint(self):
        series = pd.Series(["LIC20240001", "CERT12345"] * 40)
        assert detect_license_num(series, "random_col") is None

    def test_vehicle_id_fires_on_vin_format_with_hint(self):
        # Real VINs - 17 chars, no I/O/Q per ISO 3779.
        series = pd.Series(["1HGCM82633A004352", "2T1BURHE0JC043821"] * 40)
        assert detect_vehicle_id(series, "vin") is not None

    def test_vehicle_id_fires_without_hint_on_vin_format(self):
        # VIN format is distinctive enough (17 chars, restricted charset).
        series = pd.Series(["1HGCM82633A004352", "2T1BURHE0JC043821"] * 40)
        assert detect_vehicle_id(series, "arbitrary_col") is not None

    def test_device_id_fires_on_device_id_col(self):
        series = pd.Series(["DEV20240012", "SN001234567"] * 40)
        assert detect_device_id(series, "device_id") is not None

    def test_device_id_silent_without_hint(self):
        series = pd.Series(["DEV20240012", "SN001234567"] * 40)
        assert detect_device_id(series, "some_col") is None

    def test_biometric_id_fires_on_fingerprint_col(self):
        series = pd.Series(["fp_hash_abc123def456", "fp_scan_0012"] * 40)
        assert detect_biometric_id(series, "fingerprint") is not None

    def test_biometric_id_silent_without_hint(self):
        series = pd.Series(["fp_hash_abc123def456", "fp_scan_0012"] * 40)
        assert detect_biometric_id(series, "col_name") is None
