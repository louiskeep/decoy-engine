"""SP-12 end-to-end detection routing tests for CPNI and P&C packs.

These tests run STORM detectors against representative column data and assert
that each column routes to the expected detector. They pin the prose coverage
claims in cpni.yaml and pc.yaml to real detection behaviour so that incorrect
name-hint configuration shows up in CI rather than at runtime.

Key assertion (HIGH fix SP-12 remediation):
  imei / imsi / iccid / handset_id columns route to device_id (fpe digits),
  NOT through as unmasked values. Before the SP-12 remediation commit these
  columns had NO device_id name hints and would have silently passed through.

Structure:
  - TestCpniDetectionRouting: representative CPNI subscriber-record frame.
  - TestPcDetectionRouting: representative P&C auto-policy frame.

Detection calls use individual detect_* functions (not run_all_detectors) so
the routing assertion is direct and failure messages are precise.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.storm.detectors import (
    detect_device_id,
    detect_email,
    detect_ssn,
    detect_us_phone,
    detect_us_zip,
    detect_vehicle_id,
    hits_name_hint,
)

# ---- CPNI detection routing --------------------------------------------------


class TestCpniDetectionRouting:
    """Verify that representative CPNI column names route to the correct detectors.

    Representative frame: subscriber master file with device identifiers.
    Before SP-12 remediation, imei / imsi / iccid columns had no device_id
    name hints and would have passed detect_device_id as None (raw passthrough).
    """

    # 15-digit strings (IMEI-shaped; also valid length for IMSI).
    _DEVICE_SERIES = pd.Series(
        ["490154203237518", "356938035643809", "012345678901230", "351756051523999"]
    )
    _SSN_SERIES = pd.Series(["123-45-6789", "555-12-3456", "111-22-3333", "444-55-6677"])
    _PHONE_SERIES = pd.Series(["(555) 234-5678", "555.234.5678", "5552345678", "(800) 555-1212"])
    _ZIP_SERIES = pd.Series(["90210", "10001", "60601", "94102"])
    _EMAIL_SERIES = pd.Series(
        ["alice@example.com", "bob@test.org", "carol@mail.net", "dave@host.io"]
    )

    # --- IMEI routing (THE KEY SP-12 REMEDIATION ASSERTION) ------------------

    def test_DR1_imei_column_routes_to_device_id(self):
        """Column named 'imei' fires detect_device_id (not None passthrough).

        This is the primary honesty assertion for the SP-12 HIGH finding:
        before the remediation, 'imei' was absent from the device_id name-hint
        list in healthcare.yaml, so this column passed through as raw IMEI.

        After remediation: 'imei' is a device_id name hint; detect_device_id
        returns a match with detector_id='device_id'.
        """
        m = detect_device_id(self._DEVICE_SERIES, "imei")
        assert m is not None, (
            "CRITICAL: column named 'imei' must fire detect_device_id. "
            "Before SP-12 remediation, 'imei' was absent from device_id name hints "
            "and raw IMEIs passed through. Check healthcare.yaml device_id patterns."
        )
        assert m.detector_id == "device_id", (
            f"'imei' column must route to 'device_id'; got {m.detector_id!r}."
        )

    def test_DR2_imsi_column_routes_to_device_id(self):
        """Column named 'imsi' fires detect_device_id.

        IMSI (International Mobile Subscriber Identity) is now a device_id
        name hint. Before SP-12 remediation, 'imsi' did not auto-route.
        """
        m = detect_device_id(self._DEVICE_SERIES, "imsi")
        assert m is not None, (
            "Column named 'imsi' must fire detect_device_id after SP-12 remediation. "
            "Verify 'imsi' is in the device_id patterns in healthcare.yaml."
        )
        assert m.detector_id == "device_id", (
            f"'imsi' column must route to 'device_id'; got {m.detector_id!r}."
        )

    def test_DR3_iccid_column_routes_to_device_id(self):
        """Column named 'iccid' fires detect_device_id.

        ICCID (Integrated Circuit Card Identifier) is now a device_id name hint.
        Before SP-12 remediation, 'iccid' did not auto-route.
        """
        m = detect_device_id(self._DEVICE_SERIES, "iccid")
        assert m is not None, (
            "Column named 'iccid' must fire detect_device_id after SP-12 remediation. "
            "Verify 'iccid' is in the device_id patterns in healthcare.yaml."
        )
        assert m.detector_id == "device_id", (
            f"'iccid' column must route to 'device_id'; got {m.detector_id!r}."
        )

    def test_DR4_handset_id_column_routes_to_device_id(self):
        """Column named 'handset_id' fires detect_device_id."""
        m = detect_device_id(self._DEVICE_SERIES, "handset_id")
        assert m is not None, (
            "Column named 'handset_id' must fire detect_device_id. "
            "Verify 'handset_id' is in the device_id patterns in healthcare.yaml."
        )
        assert m.detector_id == "device_id", (
            f"'handset_id' column must route to 'device_id'; got {m.detector_id!r}."
        )

    def test_DR5_imei_number_variant_routes_to_device_id(self):
        """Column named 'imei_number' fires detect_device_id."""
        m = detect_device_id(self._DEVICE_SERIES, "imei_number")
        assert m is not None, (
            "Column named 'imei_number' must fire detect_device_id. "
            "Verify 'imei_number' is in the device_id patterns in healthcare.yaml."
        )
        assert m.detector_id == "device_id"

    def test_DR6_device_imei_variant_routes_to_device_id(self):
        """Column named 'device_imei' fires detect_device_id."""
        m = detect_device_id(self._DEVICE_SERIES, "device_imei")
        assert m is not None, (
            "Column named 'device_imei' must fire detect_device_id. "
            "Verify 'device_imei' is in the device_id patterns in healthcare.yaml."
        )
        assert m.detector_id == "device_id"

    # --- Pre-existing device_id hints (regression: must not break) -----------

    def test_DR7_existing_device_id_hint_still_fires(self):
        """Column named 'device_id' still routes to device_id (regression guard)."""
        m = detect_device_id(self._DEVICE_SERIES, "device_id")
        assert m is not None, "Existing 'device_id' hint must still fire after SP-12 edit."
        assert m.detector_id == "device_id"

    def test_DR8_serial_number_hint_still_fires(self):
        """Column named 'serial_number' still routes to device_id (regression guard)."""
        m = detect_device_id(self._DEVICE_SERIES, "serial_number")
        assert m is not None, "Existing 'serial_number' hint must still fire."
        assert m.detector_id == "device_id"

    # --- Other CPNI columns route correctly ----------------------------------

    def test_DR9_subscriber_ssn_routes_to_ssn(self):
        """Column named 'subscriber_ssn' routes to ssn."""
        m = detect_ssn(self._SSN_SERIES, "subscriber_ssn")
        assert m is not None, "Column 'subscriber_ssn' must fire detect_ssn."
        assert m.detector_id == "ssn"

    def test_DR10_calling_number_is_not_a_us_phone_name_hint(self):
        """Column named 'calling_number' does NOT route to us_phone (not a US-phone hint).

        CDR calling/called numbers use value-pattern detection; the column name
        itself ('calling_number') is not a registered us_phone name hint. The
        detector relies on the value pattern matching E.164 NANP numbers.
        This test pins the routing behaviour so a false hint addition is visible.
        """
        # calling_number is not a us_phone name hint; detection is value-pattern only.
        # The point here is that the column name alone does not guarantee routing --
        # no false name-hint should be claimed. (Value-pattern detection is separate
        # and not the routing assertion we are testing here.)
        assert not hits_name_hint("us_phone", "calling_number"), (
            "'calling_number' must NOT be a us_phone name hint. "
            "US phone routing for CDR columns should rely on value pattern, not column name."
        )

    def test_DR11_billing_zip_routes_to_us_zip(self):
        """Column named 'billing_zip' routes to us_zip."""
        m = detect_us_zip(self._ZIP_SERIES, "billing_zip")
        assert m is not None, "Column 'billing_zip' must fire detect_us_zip."
        assert m.detector_id == "us_zip"

    def test_DR12_subscriber_email_routes_to_email(self):
        """Column named 'subscriber_email' routes to email."""
        m = detect_email(self._EMAIL_SERIES, "subscriber_email")
        assert m is not None, "Column 'subscriber_email' must fire detect_email."
        assert m.detector_id == "email"

    # --- Name-hint isolation: non-telecom generic column must not fire --------

    def test_DR13_transaction_id_does_not_fire_device_id(self):
        """A column named 'transaction_id' must NOT fire detect_device_id.

        device_id is name-hint-only; 'transaction_id' contains 'id' but not a
        device_id-specific token, so the detector must stay silent.
        """
        m = detect_device_id(self._DEVICE_SERIES, "transaction_id")
        assert m is None, (
            "Column 'transaction_id' must NOT fire detect_device_id. "
            "This would be a false positive: 'id' is too generic to be a device hint."
        )


# ---- P&C detection routing --------------------------------------------------


class TestPcDetectionRouting:
    """Verify that representative P&C auto-policy column names route correctly.

    Representative frame: auto-policy record with VIN, SSN, phone, zip, email.
    """

    _VIN_SERIES = pd.Series(
        ["1HGCM82633A004352", "4T1BF3EK5U1234567", "JN1AZ4EHXM1234567", "3VWFE21C04M000001"]
    )
    _SSN_SERIES = pd.Series(["123-45-6789", "555-12-3456", "111-22-3333", "444-55-6677"])
    _PHONE_SERIES = pd.Series(["(555) 234-5678", "555.234.5678", "5552345678", "(800) 555-1212"])
    _ZIP_SERIES = pd.Series(["90210", "10001", "60601", "94102"])
    _EMAIL_SERIES = pd.Series(
        ["alice@example.com", "bob@test.org", "carol@mail.net", "dave@host.io"]
    )

    def test_PC_DR1_vin_column_routes_to_vehicle_id(self):
        """Column named 'vin' routes to vehicle_id."""
        m = detect_vehicle_id(self._VIN_SERIES, "vin")
        assert m is not None, "Column named 'vin' must fire detect_vehicle_id."
        assert m.detector_id == "vehicle_id", (
            f"'vin' column must route to 'vehicle_id'; got {m.detector_id!r}."
        )

    def test_PC_DR2_policyholder_ssn_routes_to_ssn(self):
        """Column named 'policyholder_ssn' routes to ssn."""
        m = detect_ssn(self._SSN_SERIES, "policyholder_ssn")
        assert m is not None, "Column 'policyholder_ssn' must fire detect_ssn."
        assert m.detector_id == "ssn"

    def test_PC_DR3_contact_phone_routes_to_us_phone(self):
        """Column named 'contact_phone' routes to us_phone (name hint + value pattern)."""
        m = detect_us_phone(self._PHONE_SERIES, "contact_phone")
        assert m is not None, "Column 'contact_phone' must fire detect_us_phone."
        assert m.detector_id == "us_phone"

    def test_PC_DR4_billing_zip_routes_to_us_zip(self):
        """Column named 'billing_zip' routes to us_zip."""
        m = detect_us_zip(self._ZIP_SERIES, "billing_zip")
        assert m is not None, "Column 'billing_zip' must fire detect_us_zip."
        assert m.detector_id == "us_zip"

    def test_PC_DR5_email_routes_to_email(self):
        """Column named 'email' routes to email detector."""
        m = detect_email(self._EMAIL_SERIES, "email")
        assert m is not None, "Column 'email' must fire detect_email."
        assert m.detector_id == "email"

    def test_PC_DR6_vehicle_id_column_routes_to_vehicle_id(self):
        """Column named 'vehicle_id' routes to vehicle_id (not device_id)."""
        m = detect_vehicle_id(self._VIN_SERIES, "vehicle_id")
        assert m is not None, "Column 'vehicle_id' must fire detect_vehicle_id."
        assert m.detector_id == "vehicle_id"
        # Confirm it does NOT also fire device_id (separate detector).
        from decoy_engine.storm.detectors import detect_device_id as _d

        device_match = _d(self._VIN_SERIES, "vehicle_id")
        assert device_match is None, (
            "Column 'vehicle_id' must NOT fire detect_device_id. "
            "'vehicle_id' is a vehicle_id hint, not a device_id hint."
        )
