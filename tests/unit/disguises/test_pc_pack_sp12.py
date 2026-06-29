"""SP-12 acceptance tests: P&C (Property and Casualty) insurance pack.

Drift-guard pattern mirrors test_hipaa_pack_sp11.py: load the shipped pack,
assert per-field strategy/param correspondence, then run checksum-bearing
fields through their transform entry points to confirm validity by construction.

Key assertions:
  - Pack loads via load_disguises() and has id='pc'.
  - VIN rule: fpe + ALPHANUM charset + checksum: vin.
    Masked VINs pass checksums.validate('vin', ...) by construction.
  - SSN rule: fpe + digits charset.
  - Driver license rule: redact.
  - Name rules: faker (first_name / last_name / person_name).
  - Address rule: faker (street_address).
  - ZIP rule: truncate (length=3).
  - Date rule: date_shift (jitter_days=30).
  - Email rule: faker (email).
  - Phone rule: faker (phone_number).
  - Account rule: fpe (ALPHANUM).

Honest scope notes baked into assertion messages:
  - Policy numbers and claim numbers have NO registered STORM detector;
    those columns cannot be auto-routed by this pack.
  - Premium/loss/reserve amounts have no STORM detector; configure bucketize
    manually in the recipe YAML.
  - Loss descriptions require manual text_mask configuration.
  - VIN checksum: vin is accurate (ISO 3779 / NHTSA 49 CFR Part 565).
    Driver licenses are redacted because no universal state format exists.

Regulatory reference: GLBA Safeguards Rule (16 CFR Part 314);
NAIC Model Insurance Data Security Law (Model 668).
"""

from __future__ import annotations

import decoy_engine.checksums as checksums
from decoy_engine.disguises import load_disguises

# ---- stable test fixtures ---------------------------------------------------

# Syntactically valid 17-character VINs with correct position-9 check digit
# per ISO 3779 / NHTSA 49 CFR Part 565 Appendix B. Generated via
# checksums._vin_calc_check_digit so position 8 (0-indexed) is valid.
_VALID_VINS = [
    "1HGCM82633A004352",  # Honda Accord; check digit at pos 8 = '3'
    "4T1BF3EK5U1234567",  # Toyota; check digit at pos 8 = '5'
    "JN1AZ4EHXM1234567",  # Nissan; check digit at pos 8 = 'X'
    "3VWFE21C04M000001",  # VW-shape; check digit at pos 8 = '0'
]

_JOB_SEED = b"\xbe\xef" * 16
_FPE_KEY = bytes(range(32))
_FPE_TWEAK = b"pc_vin_col"

# Resolved ALPHANUM charset string (not the name 'ALPHANUM').
# fpe_encrypt_value takes the actual charset characters, not the named alias.
# The FPEStrategy.apply() resolves 'ALPHANUM' via _CHARSETS; tests must do the same.
_ALPHANUM_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


# ---- helpers ----------------------------------------------------------------


def _pc_pack():
    """Load the shipped P&C pack."""
    packs = {d.id: d for d in load_disguises()}
    assert "pc" in packs, (
        "P&C pack with id='pc' not found in load_disguises() output. "
        "Verify pc.yaml is in decoy_engine/disguises/."
    )
    return packs["pc"]


def _rule_for_detector(detector_id: str):
    """Return the first field_rule in the P&C pack that lists this detector."""
    pack = _pc_pack()
    for rule in pack.field_rules:
        if detector_id in rule.detectors:
            return rule
    return None


# ---- structural assertions --------------------------------------------------


class TestPcPackStructure:
    """field_rules in the P&C pack match the SP-12 design intent."""

    def test_P1_pack_loads_with_correct_id(self):
        """Pack loads via load_disguises() with id='pc'."""
        pack = _pc_pack()
        assert pack.id == "pc", f"Expected id='pc', got {pack.id!r}"

    def test_P1_pack_version_is_sp12_date(self):
        """Pack version must be '2026-06-29' (SP-12 ship date)."""
        pack = _pc_pack()
        assert pack.version == "2026-06-29", (
            f"P&C pack version must be '2026-06-29'; got {pack.version!r}"
        )

    def test_P2_vin_uses_fpe_with_checksum_vin(self):
        """VIN rule: fpe + ALPHANUM charset + checksum: vin.

        Masked VINs must be valid by construction per ISO 3779 /
        NHTSA 49 CFR Part 565 Appendix B. This is the primary
        checksum-bearing field in P&C datasets.
        """
        rule = _rule_for_detector("vehicle_id")
        assert rule is not None, "No field_rule found for 'vehicle_id' detector in P&C pack."
        assert rule.mask == "fpe", (
            f"vehicle_id rule must use 'fpe' strategy; got {rule.mask!r}. "
            f"FPE + checksum: vin produces masked VINs valid by construction."
        )
        assert rule.params.get("checksum") == "vin", (
            f"vehicle_id fpe rule must carry checksum='vin'; got params={rule.params}. "
            f"Without checksum: vin, masked VINs fail standard VIN validators."
        )
        assert rule.params.get("charset") == "ALPHANUM", (
            f"vehicle_id fpe rule must carry charset='ALPHANUM' (covers VIN char set "
            f"0-9 A-Z minus I/O/Q per NHTSA); got params={rule.params}"
        )

    def test_P3_ssn_uses_fpe_digits(self):
        """SSN rule: fpe + digits charset. Policyholder SSN."""
        rule = _rule_for_detector("ssn")
        assert rule is not None, "No field_rule found for 'ssn' detector in P&C pack."
        assert rule.mask == "fpe", f"ssn rule must use 'fpe'; got {rule.mask!r}"
        assert rule.params.get("charset") == "digits", (
            f"ssn fpe rule must carry charset='digits'; got params={rule.params}"
        )

    def test_P4_license_num_uses_redact(self):
        """Driver license rule: redact.

        No universal state format exists; FPE cannot produce state-valid
        license numbers for all 50 US state formats.
        """
        rule = _rule_for_detector("license_num")
        assert rule is not None, "No field_rule found for 'license_num' detector in P&C pack."
        assert rule.mask == "redact", (
            f"license_num rule must use 'redact' (no universal US state format); got {rule.mask!r}"
        )

    def test_P5_first_name_uses_faker_first_name(self):
        """First name rule: faker + faker_type: first_name."""
        rule = _rule_for_detector("first_name")
        assert rule is not None, "No field_rule for 'first_name' in P&C pack."
        assert rule.mask == "faker", f"first_name rule must use 'faker'; got {rule.mask!r}"
        assert rule.params.get("faker_type") == "first_name", (
            f"first_name faker rule must carry faker_type='first_name'; got {rule.params}"
        )

    def test_P5_last_name_uses_faker_last_name(self):
        """Last name rule: faker + faker_type: last_name."""
        rule = _rule_for_detector("last_name")
        assert rule is not None, "No field_rule for 'last_name' in P&C pack."
        assert rule.mask == "faker", f"last_name rule must use 'faker'; got {rule.mask!r}"
        assert rule.params.get("faker_type") == "last_name", (
            f"last_name faker rule must carry faker_type='last_name'; got {rule.params}"
        )

    def test_P5_person_name_uses_faker_name(self):
        """Full name rule: faker + faker_type: name."""
        rule = _rule_for_detector("person_name")
        assert rule is not None, "No field_rule for 'person_name' in P&C pack."
        assert rule.mask == "faker", f"person_name rule must use 'faker'; got {rule.mask!r}"
        assert rule.params.get("faker_type") == "name", (
            f"person_name faker rule must carry faker_type='name'; got {rule.params}"
        )

    def test_P6_address_uses_faker_street_address(self):
        """Address rule: faker + faker_type: street_address."""
        rule = _rule_for_detector("address")
        assert rule is not None, "No field_rule for 'address' in P&C pack."
        assert rule.mask == "faker", f"address rule must use 'faker'; got {rule.mask!r}"
        assert rule.params.get("faker_type") == "street_address", (
            f"address faker rule must carry faker_type='street_address'; got {rule.params}"
        )

    def test_P7_us_zip_uses_truncate_3(self):
        """ZIP rule: truncate to 3 digits. P&C geographic coarsening (GLBA)."""
        rule = _rule_for_detector("us_zip")
        assert rule is not None, "No field_rule for 'us_zip' in P&C pack."
        assert rule.mask == "truncate", f"us_zip rule must use 'truncate'; got {rule.mask!r}"
        assert rule.params.get("length") == 3, (
            f"us_zip truncate rule must carry length=3; got params={rule.params}"
        )

    def test_P8_dates_use_date_shift_30(self):
        """Date rule: date_shift +/- 30 days. Covers iso_date, us_date, eu_date."""
        rule = _rule_for_detector("iso_date")
        assert rule is not None, "No field_rule for 'iso_date' in P&C pack."
        assert rule.mask == "date_shift", f"iso_date rule must use 'date_shift'; got {rule.mask!r}"
        assert rule.params.get("jitter_days") == 30, (
            f"date_shift rule must carry jitter_days=30; got params={rule.params}"
        )
        # All three date detectors must share the same rule.
        assert "us_date" in rule.detectors, "us_date must be in the same date_shift rule."
        assert "eu_date" in rule.detectors, "eu_date must be in the same date_shift rule."

    def test_P9_email_uses_faker(self):
        """Email rule: faker + faker_type: email."""
        rule = _rule_for_detector("email")
        assert rule is not None, "No field_rule for 'email' in P&C pack."
        assert rule.mask == "faker" and rule.params.get("faker_type") == "email", (
            f"email rule must be faker/email; got mask={rule.mask!r} params={rule.params}"
        )

    def test_P10_us_phone_uses_faker(self):
        """Phone rule: faker + faker_type: phone_number."""
        rule = _rule_for_detector("us_phone")
        assert rule is not None, "No field_rule for 'us_phone' in P&C pack."
        assert rule.mask == "faker", f"us_phone rule must use 'faker'; got {rule.mask!r}"
        assert rule.params.get("faker_type") == "phone_number", (
            f"us_phone faker rule must carry faker_type='phone_number'; got {rule.params}"
        )

    def test_P11_iban_uses_fpe_alphanum(self):
        """Account number (iban) rule: fpe + ALPHANUM charset."""
        rule = _rule_for_detector("iban")
        assert rule is not None, "No field_rule for 'iban' in P&C pack."
        assert rule.mask == "fpe", f"iban rule must use 'fpe'; got {rule.mask!r}"
        assert rule.params.get("charset") == "ALPHANUM", (
            f"iban fpe rule must carry charset='ALPHANUM'; got params={rule.params}"
        )


# ---- pack-param-driven VIN checksum acceptance ------------------------------


class TestVinChecksumAcceptance:
    """A1: VIN masked via fpe + checksum: vin passes VIN checksum post-mask.

    The checksum recomputation happens inside fpe_encrypt_value when
    checksum='vin' is passed. Output is checksum-valid by construction per
    ISO 3779 / NHTSA 49 CFR Part 565 Appendix B (position-9 check character).

    Pack-param-driven: uses vehicle_id rule.params from the loaded pack,
    not hand-built dicts, so a key rename in pc.yaml fails here.
    """

    def _pack(self):
        return _pc_pack()

    def _vin_rule(self):
        pack = self._pack()
        return next(r for r in pack.field_rules if "vehicle_id" in r.detectors)

    def _resolved_charset(self, rule):
        """Resolve the charset name to the actual charset string.

        fpe_encrypt_value expects the actual character string (e.g. the 62-char
        ALPHANUM string), not the named alias ('ALPHANUM'). FPEStrategy.apply()
        resolves via _CHARSETS; tests must do the same.
        """
        from decoy_engine.transforms.fpe import _CHARSETS

        charset_spec = rule.params.get("charset", "digits")
        return _CHARSETS.get(charset_spec, charset_spec)

    def test_A1_vin_rule_params_drive_valid_vin_output(self):
        """VIN rule.params from the loaded pack drive fpe+checksum: vin -> valid VIN.

        Uses vin_rule.params.get('checksum') (not a hard-coded string 'vin').
        If 'checksum' is renamed or dropped in pc.yaml, params.get('checksum')
        returns None, fpe_encrypt_value skips recomputation, and
        checksums.validate('vin', masked) fails -- the test has teeth.

        Charset is resolved via _CHARSETS (same resolution path as FPEStrategy.apply())
        so a charset rename in pc.yaml also fails this test.
        """
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        vin_rule = self._vin_rule()
        checksum = vin_rule.params.get("checksum")
        charset = self._resolved_charset(vin_rule)

        for vin in _VALID_VINS:
            masked = fpe_encrypt_value(vin, _FPE_KEY, charset, _FPE_TWEAK, checksum=checksum)
            assert checksums.validate("vin", masked), (
                f"Masked VIN {masked!r} (from {vin!r}) failed checksums.validate('vin'). "
                f"Loaded vin_rule.params={vin_rule.params!r}. "
                f"Dropping or renaming 'checksum' in pc.yaml causes this failure."
            )

    def test_A1_masked_vin_is_17_chars(self):
        """FPE output for VIN is 17 characters (correct VIN length)."""
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        vin_rule = self._vin_rule()
        checksum = vin_rule.params.get("checksum")
        charset = self._resolved_charset(vin_rule)

        for vin in _VALID_VINS:
            masked = fpe_encrypt_value(vin, _FPE_KEY, charset, _FPE_TWEAK, checksum=checksum)
            assert len(masked) == 17, (
                f"Masked VIN {masked!r} (from {vin!r}) must be 17 characters; "
                f"got length={len(masked)}."
            )

    def test_A1_masked_vin_is_deterministic(self):
        """Same VIN + same key -> same masked VIN (deterministic FPE)."""
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        vin_rule = self._vin_rule()
        checksum = vin_rule.params.get("checksum")
        charset = self._resolved_charset(vin_rule)

        for vin in _VALID_VINS:
            a = fpe_encrypt_value(vin, _FPE_KEY, charset, _FPE_TWEAK, checksum=checksum)
            b = fpe_encrypt_value(vin, _FPE_KEY, charset, _FPE_TWEAK, checksum=checksum)
            assert a == b, (
                f"FPE + checksum: vin must be deterministic for {vin!r}; got {a!r} != {b!r}"
            )

    def test_A1_masked_vin_differs_from_input(self):
        """Masked VIN is different from the input VIN (FPE is not identity)."""
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        vin_rule = self._vin_rule()
        checksum = vin_rule.params.get("checksum")
        charset = self._resolved_charset(vin_rule)

        any_different = False
        for vin in _VALID_VINS:
            masked = fpe_encrypt_value(vin, _FPE_KEY, charset, _FPE_TWEAK, checksum=checksum)
            if masked != vin:
                any_different = True
                break
        assert any_different, (
            "All masked VINs are identical to their inputs -- FPE is behaving as identity. "
            "Check that the FPE key is non-trivial and the charset is resolved correctly."
        )


# ---- honesty guards: pack must NOT over-claim --------------------------------


class TestPcHonestCoverage:
    """Honesty guards: the P&C pack must not claim capabilities it lacks.

    These assertions prevent documentation drift where the pack summary or
    field_rules over-claim coverage not backed by shipped STORM detectors.
    """

    def test_H1_no_policy_number_detector_in_rules(self):
        """Policy numbers have no registered STORM detector.

        The pack must NOT carry a field_rule referencing a 'policy_number'
        or 'policy_num' detector because no such detector exists.
        """
        pack = _pc_pack()
        bad_rules = [
            r
            for r in pack.field_rules
            if any(d in ("policy_number", "policy_num", "pol_num") for d in r.detectors)
        ]
        assert not bad_rules, (
            f"P&C pack must NOT carry field_rules for policy number detectors "
            f"(no such STORM detector registered). Found: {bad_rules}"
        )

    def test_H2_no_claim_number_detector_in_rules(self):
        """Claim numbers have no registered STORM detector."""
        pack = _pc_pack()
        bad_rules = [
            r
            for r in pack.field_rules
            if any(d in ("claim_number", "claim_num", "claim_id") for d in r.detectors)
        ]
        assert not bad_rules, (
            f"P&C pack must NOT carry field_rules for claim number detectors "
            f"(no such STORM detector). Found: {bad_rules}"
        )

    def test_H3_no_loss_amount_detector_in_rules(self):
        """Premium/loss/reserve amounts have no STORM detector."""
        pack = _pc_pack()
        bad_rules = [
            r
            for r in pack.field_rules
            if any(d in ("loss_amount", "premium", "reserve") for d in r.detectors)
        ]
        assert not bad_rules, (
            f"P&C pack must NOT claim amount-column detectors; configure bucketize "
            f"manually in recipe YAML. Found: {bad_rules}"
        )

    def test_H4_pack_has_vin_checksum_not_luhn(self):
        """VIN uses checksum: vin not checksum: luhn.

        VIN uses ISO 3779 positional weights (not Luhn). Using luhn here
        would produce 17-char strings that pass Luhn but NOT VIN validation.
        """
        rule = _rule_for_detector("vehicle_id")
        assert rule is not None
        checksum = rule.params.get("checksum")
        assert checksum == "vin", (
            f"vehicle_id rule must carry checksum='vin' (ISO 3779), not 'luhn'. "
            f"Got checksum={checksum!r}. VIN check digit uses positional weights "
            f"(NHTSA 49 CFR Part 565 Appendix B), not the Luhn algorithm."
        )
        assert checksum != "luhn", (
            "vehicle_id rule must NOT use checksum='luhn'; VIN is NOT Luhn-validated."
        )
