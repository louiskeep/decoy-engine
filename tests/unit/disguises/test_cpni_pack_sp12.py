"""SP-12 acceptance tests: CPNI (Customer Proprietary Network Information) telecom pack.

Drift-guard pattern mirrors test_hipaa_pack_sp11.py: load the shipped pack,
assert per-field strategy/param correspondence, then test the checksum-adjacent
field (device_id / fpe digits) and confirm the honest gap for IMEI Luhn is
documented but NOT auto-claimed by the pack.

Key assertions:
  - Pack loads via load_disguises() and has id='cpni'.
  - SSN rule: fpe + digits charset.
  - Phone rule (us_phone): faker + phone_number.
  - Device ID rule (device_id): fpe + digits charset WITHOUT checksum.
    The pack must NOT carry checksum: luhn for device_id because the detector
    catches non-IMEI identifiers (IMSI, ICCID) where Luhn is incorrect.
  - Name rules: faker (first_name / last_name / person_name).
  - Address rule: faker street_address.
  - ZIP rule: truncate (length=3).
  - Date rule: date_shift (jitter_days=30).
  - Email rule: faker email.
  - Account rule (iban): fpe ALPHANUM.

Honest-gap guards:
  - Pack does NOT carry a field_rule for 'imei', 'imsi', 'iccid', 'msisdn'
    (no such STORM detectors exist).
  - Pack does NOT carry checksum: luhn on the device_id rule (would over-claim
    IMEI-specific Luhn for IMSI/ICCID columns that also match device_id).
  - Cell tower lat/lng has no detector; pack must not claim to auto-route it.

Regulatory reference: 47 CFR Part 64 Subpart U (47 U.S.C. 222).
"""

from __future__ import annotations

from decoy_engine.disguises import load_disguises

# ---- helpers ----------------------------------------------------------------


def _cpni_pack():
    """Load the shipped CPNI pack."""
    packs = {d.id: d for d in load_disguises()}
    assert "cpni" in packs, (
        "CPNI pack with id='cpni' not found in load_disguises() output. "
        "Verify cpni.yaml is in decoy_engine/disguises/."
    )
    return packs["cpni"]


def _rule_for_detector(detector_id: str):
    """Return the first field_rule in the CPNI pack that lists this detector."""
    pack = _cpni_pack()
    for rule in pack.field_rules:
        if detector_id in rule.detectors:
            return rule
    return None


# ---- structural assertions --------------------------------------------------


class TestCpniPackStructure:
    """field_rules in the CPNI pack match the SP-12 design intent."""

    def test_C1_pack_loads_with_correct_id(self):
        """Pack loads via load_disguises() with id='cpni'."""
        pack = _cpni_pack()
        assert pack.id == "cpni", f"Expected id='cpni', got {pack.id!r}"

    def test_C1_pack_version_is_sp12_date(self):
        """Pack version must be '2026-06-29' (SP-12 ship date)."""
        pack = _cpni_pack()
        assert pack.version == "2026-06-29", (
            f"CPNI pack version must be '2026-06-29'; got {pack.version!r}"
        )

    def test_C2_ssn_uses_fpe_digits(self):
        """SSN rule: fpe + digits charset. Subscriber SSN."""
        rule = _rule_for_detector("ssn")
        assert rule is not None, "No field_rule for 'ssn' in CPNI pack."
        assert rule.mask == "fpe", f"ssn rule must use 'fpe'; got {rule.mask!r}"
        assert rule.params.get("charset") == "digits", (
            f"ssn fpe rule must carry charset='digits'; got params={rule.params}"
        )

    def test_C3_us_phone_uses_faker(self):
        """US phone rule: faker + phone_number.

        Covers subscriber MSISDN and US-format CDR calling/called numbers.
        47 CFR 64.2003: call detail information includes called/calling numbers.
        NOTE: FK joins across CDR tables require fpe+digits manually.
        """
        rule = _rule_for_detector("us_phone")
        assert rule is not None, "No field_rule for 'us_phone' in CPNI pack."
        assert rule.mask == "faker", f"us_phone rule must use 'faker'; got {rule.mask!r}"
        assert rule.params.get("faker_type") == "phone_number", (
            f"us_phone faker rule must carry faker_type='phone_number'; got {rule.params}"
        )

    def test_C4_device_id_uses_fpe_digits_without_checksum(self):
        """Device ID rule: fpe + digits charset WITHOUT checksum.

        The device_id detector matches IMEI, IMSI, ICCID, and other device
        serial numbers. Luhn is accurate ONLY for 15-digit IMEI; IMSI has
        no checksum, ICCID has no registered scheme.

        This assertion verifies the pack does NOT over-claim Luhn validity
        for all device_id hits. IMEI-specific columns must add checksum: luhn
        manually in the recipe YAML.

        Reference: SP-12 honesty requirement; 47 CFR 64.2003 (device identifiers
        are CPNI when they relate to service configuration and type).
        """
        rule = _rule_for_detector("device_id")
        assert rule is not None, (
            "No field_rule for 'device_id' in CPNI pack. "
            "IMEI/IMSI columns should be detected via device_id name hints."
        )
        assert rule.mask == "fpe", (
            f"device_id rule must use 'fpe' for format-preserving pseudonymisation; "
            f"got {rule.mask!r}"
        )
        assert rule.params.get("charset") == "digits", (
            f"device_id fpe rule must carry charset='digits' (IMEI/IMSI are digit-only); "
            f"got params={rule.params}"
        )
        # THE CRITICAL HONESTY ASSERTION: no Luhn over-claim.
        assert "checksum" not in rule.params, (
            f"CRITICAL: device_id rule must NOT carry checksum (would over-claim Luhn "
            f"validity for IMSI/ICCID columns that also match device_id). "
            f"For 15-digit IMEI columns, add checksum: luhn in the recipe YAML. "
            f"Got params={rule.params!r}. "
            f"SP-12 honesty requirement: map ONLY to strategies that genuinely achieve "
            f"the de-identification the regulation needs."
        )

    def test_C5_first_name_uses_faker_first_name(self):
        """First name rule: faker + first_name."""
        rule = _rule_for_detector("first_name")
        assert rule is not None, "No field_rule for 'first_name' in CPNI pack."
        assert rule.mask == "faker" and rule.params.get("faker_type") == "first_name", (
            f"first_name rule must be faker/first_name; got {rule.mask!r} {rule.params}"
        )

    def test_C5_last_name_uses_faker_last_name(self):
        """Last name rule: faker + last_name."""
        rule = _rule_for_detector("last_name")
        assert rule is not None, "No field_rule for 'last_name' in CPNI pack."
        assert rule.mask == "faker" and rule.params.get("faker_type") == "last_name", (
            f"last_name rule must be faker/last_name; got {rule.mask!r} {rule.params}"
        )

    def test_C5_person_name_uses_faker_name(self):
        """Full name rule: faker + name."""
        rule = _rule_for_detector("person_name")
        assert rule is not None, "No field_rule for 'person_name' in CPNI pack."
        assert rule.mask == "faker" and rule.params.get("faker_type") == "name", (
            f"person_name rule must be faker/name; got {rule.mask!r} {rule.params}"
        )

    def test_C6_address_uses_faker_street_address(self):
        """Address rule: faker + street_address."""
        rule = _rule_for_detector("address")
        assert rule is not None, "No field_rule for 'address' in CPNI pack."
        assert rule.mask == "faker" and rule.params.get("faker_type") == "street_address", (
            f"address rule must be faker/street_address; got {rule.mask!r} {rule.params}"
        )

    def test_C7_us_zip_uses_truncate_3(self):
        """ZIP rule: truncate to 3 digits. Billing ZIP geographic coarsening."""
        rule = _rule_for_detector("us_zip")
        assert rule is not None, "No field_rule for 'us_zip' in CPNI pack."
        assert rule.mask == "truncate", f"us_zip rule must use 'truncate'; got {rule.mask!r}"
        assert rule.params.get("length") == 3, (
            f"us_zip truncate rule must carry length=3; got params={rule.params}"
        )

    def test_C8_dates_use_date_shift_30(self):
        """Date rule: date_shift +/- 30 days."""
        rule = _rule_for_detector("iso_date")
        assert rule is not None, "No field_rule for 'iso_date' in CPNI pack."
        assert rule.mask == "date_shift", f"iso_date rule must use 'date_shift'; got {rule.mask!r}"
        assert rule.params.get("jitter_days") == 30, (
            f"date_shift rule must carry jitter_days=30; got {rule.params}"
        )
        assert "us_date" in rule.detectors, "us_date must be in the same date_shift rule."
        assert "eu_date" in rule.detectors, "eu_date must be in the same date_shift rule."

    def test_C9_email_uses_faker(self):
        """Email rule: faker + email."""
        rule = _rule_for_detector("email")
        assert rule is not None, "No field_rule for 'email' in CPNI pack."
        assert rule.mask == "faker" and rule.params.get("faker_type") == "email", (
            f"email rule must be faker/email; got {rule.mask!r} {rule.params}"
        )

    def test_C10_iban_uses_fpe_alphanum(self):
        """Account number (iban) rule: fpe + ALPHANUM."""
        rule = _rule_for_detector("iban")
        assert rule is not None, "No field_rule for 'iban' in CPNI pack."
        assert rule.mask == "fpe", f"iban rule must use 'fpe'; got {rule.mask!r}"
        assert rule.params.get("charset") == "ALPHANUM", (
            f"iban fpe rule must carry charset='ALPHANUM'; got {rule.params}"
        )


# ---- pack-param-driven fpe acceptance (device_id) ---------------------------


class TestDeviceIdFpeAcceptance:
    """A1: device_id masked via fpe + charset: digits preserves digit format.

    No checksum is claimed; the test verifies the pack-driven params
    produce format-preserving output without Luhn recomputation.

    Pack-param-driven: uses device_id rule.params from the loaded pack so a
    key rename in cpni.yaml fails at apply time.
    """

    _FPE_KEY = bytes(range(32))
    _FPE_TWEAK = b"cpni_device_col"

    # 15-digit IMEI-shaped strings (not necessarily real registered IMEIs).
    _DEVICE_IDS = ["490154203237518", "356938035643809", "012345678901230"]

    def _device_rule(self):
        pack = _cpni_pack()
        return next(r for r in pack.field_rules if "device_id" in r.detectors)

    def _resolved_charset(self, rule):
        """Resolve the charset name to the actual charset string.

        fpe_encrypt_value expects the actual character string, not the named
        alias. 'digits' -> '0123456789'. FPEStrategy.apply() resolves via
        _CHARSETS; tests must do the same to exercise the same code path.
        """
        from decoy_engine.transforms.fpe import _CHARSETS

        charset_spec = rule.params.get("charset", "digits")
        return _CHARSETS.get(charset_spec, charset_spec)

    def test_A1_device_id_pack_params_produce_digit_string(self):
        """device_id fpe output from pack params is all digits, same length as input.

        Pack-driven: uses device_rule.params (not hard-coded). If 'charset' is
        renamed in cpni.yaml, fpe_encrypt_value uses a wrong charset and the
        digit assertion fails -- the test has teeth.

        Charset is resolved via _CHARSETS (same resolution path as FPEStrategy.apply()).
        """
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        rule = self._device_rule()
        charset = self._resolved_charset(rule)
        checksum = rule.params.get("checksum")  # must be None

        assert checksum is None, (
            f"device_id rule must NOT carry checksum; got checksum={checksum!r}. "
            f"This would apply Luhn over IMSI/ICCID hits, over-claiming validity."
        )

        for did in self._DEVICE_IDS:
            masked = fpe_encrypt_value(
                did, self._FPE_KEY, charset, self._FPE_TWEAK, checksum=checksum
            )
            assert masked.isdigit(), (
                f"fpe output for device_id {did!r} must be all digits; got {masked!r}. "
                f"Loaded rule.params={rule.params!r}."
            )
            assert len(masked) == len(did), (
                f"fpe output for device_id {did!r} must preserve length; "
                f"got len={len(masked)} (input len={len(did)})."
            )

    def test_A1_device_id_fpe_is_deterministic(self):
        """Same device_id + same key -> same masked value (deterministic FPE)."""
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        rule = self._device_rule()
        charset = self._resolved_charset(rule)
        checksum = rule.params.get("checksum")

        for did in self._DEVICE_IDS:
            a = fpe_encrypt_value(did, self._FPE_KEY, charset, self._FPE_TWEAK, checksum=checksum)
            b = fpe_encrypt_value(did, self._FPE_KEY, charset, self._FPE_TWEAK, checksum=checksum)
            assert a == b, (
                f"fpe for device_id must be deterministic for {did!r}; got {a!r} != {b!r}"
            )

    def test_A1_device_id_fpe_output_differs_from_input(self):
        """Masked device_id differs from input for at least one test value."""
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        rule = self._device_rule()
        charset = self._resolved_charset(rule)
        checksum = rule.params.get("checksum")

        any_different = False
        for did in self._DEVICE_IDS:
            masked = fpe_encrypt_value(
                did, self._FPE_KEY, charset, self._FPE_TWEAK, checksum=checksum
            )
            if masked != did:
                any_different = True
                break
        assert any_different, (
            "All masked device_ids are identical to inputs -- FPE is behaving as identity. "
            "Check that the FPE key is non-trivial and the charset resolves correctly."
        )


# ---- honesty guards ----------------------------------------------------------


class TestCpniHonestCoverage:
    """Honesty guards: the CPNI pack must not claim capabilities it lacks.

    These tests prevent documentation drift that would over-promise
    auto-routing for fields that have no STORM detectors.
    """

    def test_H1_no_imei_detector_in_rules(self):
        """IMEI has no registered STORM detector; pack must not reference it."""
        pack = _cpni_pack()
        bad = [r for r in pack.field_rules if "imei" in r.detectors]
        assert not bad, (
            f"CPNI pack must NOT carry field_rules for 'imei' detector "
            f"(no such STORM detector). Found: {bad}"
        )

    def test_H2_no_imsi_detector_in_rules(self):
        """IMSI has no registered STORM detector."""
        pack = _cpni_pack()
        bad = [r for r in pack.field_rules if "imsi" in r.detectors]
        assert not bad, (
            f"CPNI pack must NOT carry field_rules for 'imsi' detector "
            f"(no such STORM detector). Found: {bad}"
        )

    def test_H3_no_iccid_detector_in_rules(self):
        """ICCID has no registered STORM detector."""
        pack = _cpni_pack()
        bad = [r for r in pack.field_rules if "iccid" in r.detectors]
        assert not bad, (
            f"CPNI pack must NOT carry field_rules for 'iccid' detector "
            f"(no such STORM detector). Found: {bad}"
        )

    def test_H4_no_msisdn_detector_in_rules(self):
        """MSISDN (as a distinct detector) has no registered STORM detector.

        MSISDN detection is handled via us_phone value pattern for US numbers;
        international MSISDNs are an honest gap. The pack must not pretend
        a 'msisdn' detector exists.
        """
        pack = _cpni_pack()
        bad = [r for r in pack.field_rules if "msisdn" in r.detectors]
        assert not bad, (
            f"CPNI pack must NOT carry a field_rule for 'msisdn' (no such STORM "
            f"detector). International MSISDN is an honest gap; US phone numbers "
            f"route via us_phone value-pattern matching. Found: {bad}"
        )

    def test_H5_no_cell_tower_detector_in_rules(self):
        """Cell tower lat/lng has no STORM detector; pack must not claim it."""
        pack = _cpni_pack()
        cell_tower_detectors = ("cell_tower", "cell_lat", "cell_lng", "tower_location")
        bad = [r for r in pack.field_rules if any(d in cell_tower_detectors for d in r.detectors)]
        assert not bad, (
            f"CPNI pack must NOT carry field_rules for cell tower detectors "
            f"(no such STORM detectors). Configure geo_generalize: lat_lng manually. "
            f"Found: {bad}"
        )

    def test_H6_device_id_rule_has_no_checksum_key(self):
        """device_id rule must not carry checksum key.

        Luhn is accurate ONLY for 15-digit IMEI. device_id detector also
        matches IMSI (no checksum) and other device serials. Claiming Luhn
        here would falsely imply all device_id hits produce Luhn-valid output.
        """
        rule = _rule_for_detector("device_id")
        assert rule is not None, "No device_id rule found."
        assert "checksum" not in rule.params, (
            f"CRITICAL honesty guard: device_id rule must not carry 'checksum'. "
            f"Applying Luhn to IMSI (no check digit) and ICCID (scheme not registered) "
            f"would over-claim validity. Add checksum: luhn MANUALLY for IMEI columns. "
            f"Got params={rule.params!r}."
        )
