"""SP-11 acceptance tests: HIPAA pack tightened posture.

Verifies the four previously-weak HIPAA Safe Harbor columns now use the
tightened strategies: NPI (fpe + checksum: npi), ICD-10 (code_set with
chapter_preserve), ZIP (geo_generalize Safe Harbor cascade), and that
free-text clinical notes run through text_mask with unmatched_span_policy:
redact (the safe default for columns where the TIER-1 detector set may not
catch all PII).

Fixture: minimal single-table healthcare claims dataset defined inline.
No separate parquet/CSV is shipped for this minimal fixture; the full
healthcare-payer fixture is SP-13.

Honest scope notes baked into assertion messages:
- M11 differential-privacy-grade is NOT provided by any strategy here.
- LOINC code_set corpus is NOT shipped (carry-forward, SP-09 docs).
- HCPCS/NDC columns have no registered STORM detectors; the pack cannot
  route them automatically. Users must configure code_set manually.
- Free-text: text_mask masks only TIER-1 detector spans (ssn, npi, icd10,
  etc.) in the built-in path. Names and undetected text in clinical_notes
  are caught by the redact-of-unmatched default, NOT by a name detector.
  This is the SP-07 honesty lesson: the safe default redacts the unmatched
  prose rather than passing it through in the clear.

References: 45 CFR 164.514(b)(2) HIPAA Safe Harbor de-identification.
"""

from __future__ import annotations

import pandas as pd

import decoy_engine.checksums as checksums
from decoy_engine.disguises import load_disguises

# ── Stable test constants ──────────────────────────────────────────────────────

_JOB_SEED = b"\xca\xfe" * 16
_FPE_KEY = bytes(range(32))
_FPE_TWEAK = b"hipaa_npi_col"
_DIGITS = "0123456789"

# Real valid NPIs per CMS check-digit spec (prefix 80840 + 9-digit body, Luhn).
_VALID_NPIS = ["1234567893", "1679576722", "1000000004"]

# ICD-10 cardiovascular (chapter I) codes.
_CARDIOVASCULAR_CODES = ["I21.9", "I25.10", "I50.9"]

# Minimal healthcare claims fixture (inline; full fixture is SP-13).
_CLAIMS_DATA = {
    "claim_id": ["C001", "C002", "C003"],
    "provider_npi": _VALID_NPIS,
    "diagnosis_code": _CARDIOVASCULAR_CODES,
    "patient_zip": ["98101", "98102", "03601"],  # 036xx is HHS-restricted
    "date_of_birth": ["1982-03-15", "1975-11-22", "1990-06-07"],
    "clinical_notes": [
        "Patient SSN is 123-45-6789. Presented with chest pain.",
        "No SSN on file. History of hypertension. NPI: 1234567893.",
        "SSN 987-65-4320. Clinical follow-up scheduled.",
    ],
}

_CLAIMS_DF = pd.DataFrame(_CLAIMS_DATA)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _hipaa_pack():
    """Load the shipped HIPAA pack."""
    packs = {d.id: d for d in load_disguises()}
    return packs["hipaa"]


def _rule_for_detector(detector_id: str):
    """Return the first field_rule in the HIPAA pack that lists this detector."""
    pack = _hipaa_pack()
    for rule in pack.field_rules:
        if detector_id in rule.detectors:
            return rule
    return None


# ── H: Pack structural assertions ─────────────────────────────────────────────
# These are RED before the YAML update and GREEN after.


class TestHipaaPackStructure:
    """field_rules in the updated pack use the tightened strategies."""

    def test_H1_npi_uses_fpe_with_checksum_npi(self):
        """NPI rule: fpe strategy with checksum: npi param.

        Pre-update: params was {charset: digits} with no checksum key.
        Post-update: params must include checksum: npi so masked NPIs are
        valid by construction per the CMS NPPES check-digit spec.
        Reference: 45 CFR 164.514(b)(2)(i)(R) - any unique code.
        """
        rule = _rule_for_detector("npi")
        assert rule is not None, "No field_rule found for 'npi' detector in HIPAA pack."
        assert rule.mask == "fpe", f"NPI rule must use 'fpe' strategy, got {rule.mask!r}"
        assert rule.params.get("checksum") == "npi", (
            f"NPI fpe rule must carry checksum='npi'; got params={rule.params}. "
            f"Without checksum:npi, masked NPIs fail CMS validation."
        )

    def test_H2_icd10_uses_code_set_with_chapter_preserve(self):
        """ICD-10 rule: code_set strategy with chapter_preserve: true.

        Pre-update: mask=truncate, params={length: 3}.
        Post-update: mask=code_set, params includes code_set: icd10,
        chapter_preserve: true. Output is always a real ICD-10-CM code
        in the same chapter as the input.
        """
        rule = _rule_for_detector("icd10")
        assert rule is not None, "No field_rule found for 'icd10' detector in HIPAA pack."
        assert rule.mask == "code_set", (
            f"ICD-10 rule must use 'code_set' strategy, got {rule.mask!r}. "
            f"The old truncate(3) strategy is replaced per SP-11."
        )
        assert rule.params.get("code_set") == "icd10", (
            f"ICD-10 code_set rule must carry code_set='icd10'; got params={rule.params}"
        )
        assert rule.params.get("chapter_preserve") is True, (
            f"ICD-10 code_set rule must carry chapter_preserve=true; got params={rule.params}"
        )

    def test_H3_us_zip_uses_geo_generalize(self):
        """ZIP rule: geo_generalize strategy with k_threshold: 20000.

        Pre-update: mask=truncate, params={length: 3}.
        Post-update: mask=geo_generalize with HIPAA Safe Harbor cascade.
        Reference: 45 CFR 164.514(b)(2)(i)(B) HIPAA Safe Harbor.
        """
        rule = _rule_for_detector("us_zip")
        assert rule is not None, "No field_rule found for 'us_zip' detector in HIPAA pack."
        assert rule.mask == "geo_generalize", (
            f"us_zip rule must use 'geo_generalize' strategy, got {rule.mask!r}. "
            f"The old truncate(3) strategy is replaced per SP-11."
        )
        k = rule.params.get("k_threshold")
        assert k == 20000, (
            f"geo_generalize rule must carry k_threshold=20000 per HIPAA Safe Harbor "
            f"population floor (45 CFR 164.514(b)(2)(i)(B)); got k_threshold={k}"
        )

    def test_H4_pack_version_is_sp11_date(self):
        """Pack version must be bumped to 2026-06-28 for the SP-11 change."""
        pack = _hipaa_pack()
        assert pack.version == "2026-06-28", (
            f"HIPAA pack version must be '2026-06-28' after SP-11 update; got {pack.version!r}"
        )


# ── A1: NPI acceptance ─────────────────────────────────────────────────────────


class TestNpiAcceptance:
    """A1: NPI masked via fpe + checksum: npi passes NPI checksum post-mask.

    The checksum recomputation happens inside fpe_encrypt_value when
    checksum='npi' is passed; output is a valid 10-digit NPI by construction
    per the CMS NPPES check-digit spec (Luhn applied to prefix 80840 +
    9-digit NPI body). Reference: 45 CFR 164.514(b)(2) Safe Harbor.
    """

    def test_A1_masked_npi_passes_checksum_validation(self):
        """Masked NPI passes checksums.validate('npi', ...) for every test NPI."""
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        for npi in _VALID_NPIS:
            masked = fpe_encrypt_value(npi, _FPE_KEY, _DIGITS, _FPE_TWEAK, checksum="npi")
            assert checksums.validate("npi", masked), (
                f"Masked NPI {masked!r} (from {npi!r}) failed checksums.validate('npi'). "
                f"fpe+checksum:npi must produce valid NPIs by construction."
            )

    def test_A1_masked_npi_is_10_digits(self):
        """FPE output is 10 digits (correct NPI length and format)."""
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        for npi in _VALID_NPIS:
            masked = fpe_encrypt_value(npi, _FPE_KEY, _DIGITS, _FPE_TWEAK, checksum="npi")
            assert len(masked) == 10 and masked.isdigit(), (
                f"Masked NPI {masked!r} is not a 10-digit string (from {npi!r})."
            )

    def test_A1_masked_npi_is_deterministic(self):
        """Same NPI + same key produces the same masked value."""
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        for npi in _VALID_NPIS:
            a = fpe_encrypt_value(npi, _FPE_KEY, _DIGITS, _FPE_TWEAK, checksum="npi")
            b = fpe_encrypt_value(npi, _FPE_KEY, _DIGITS, _FPE_TWEAK, checksum="npi")
            assert a == b, f"FPE+checksum:npi must be deterministic for {npi!r}"


# ── A2: ICD-10 chapter acceptance ─────────────────────────────────────────────


class TestIcd10ChapterAcceptance:
    """A2: ICD-10 code_set masking preserves chapter (I-codes -> I-codes).

    A cardiovascular code (I21.9, etc.) maps to another I-chapter code, never
    to a different chapter. The output is always a real ICD-10-CM code from
    the shipped corpus (CMS ICD-10-CM, US public domain, 65 codes).
    Reference: 45 CFR 164.514(b)(2) Safe Harbor; replaces truncate(3) strategy.
    """

    def test_A2_cardiovascular_code_stays_in_I_chapter(self):
        """Each I-chapter code maps to another I-chapter code under chapter_preserve."""
        from decoy_engine.transforms.code_set import CodeSetConfig, apply_code_set

        cfg = CodeSetConfig.from_dict({"code_set": "icd10", "chapter_preserve": True})
        for code in _CARDIOVASCULAR_CODES:
            masked = apply_code_set(code, cfg, mode="mask", job_seed=_JOB_SEED)
            assert masked.startswith("I"), (
                f"Cardiovascular I-code {code!r} must mask to another I-chapter code; "
                f"got {masked!r}. chapter_preserve=true must prevent cross-chapter output."
            )

    def test_A2_masked_icd10_is_a_real_corpus_code(self):
        """Output code must be a real ICD-10-CM code from the shipped corpus."""
        from decoy_engine.transforms.code_set import CodeSetConfig, apply_code_set, load_corpus

        cfg = CodeSetConfig.from_dict({"code_set": "icd10", "chapter_preserve": True})
        corpus_codes = {row["code"] for row in load_corpus("icd10")}
        for code in _CARDIOVASCULAR_CODES:
            masked = apply_code_set(code, cfg, mode="mask", job_seed=_JOB_SEED)
            assert masked in corpus_codes, (
                f"Masked ICD-10 code {masked!r} (from {code!r}) is not in the shipped "
                f"icd10 corpus. code_set must only emit real corpus codes."
            )

    def test_A2_icd10_mask_is_deterministic(self):
        """Same input + same seed produces the same masked code."""
        from decoy_engine.transforms.code_set import CodeSetConfig, apply_code_set

        cfg = CodeSetConfig.from_dict({"code_set": "icd10", "chapter_preserve": True})
        for code in _CARDIOVASCULAR_CODES:
            a = apply_code_set(code, cfg, mode="mask", job_seed=_JOB_SEED)
            b = apply_code_set(code, cfg, mode="mask", job_seed=_JOB_SEED)
            assert a == b, f"code_set mask must be deterministic for {code!r}"


# ── A3: Free-text clinical_notes acceptance ────────────────────────────────────


class TestFreeTextMaskAcceptance:
    """A3: clinical_notes text_mask with unmatched_span_policy: redact.

    SSN and NPI spans in clinical notes are masked by the TIER-1 detectors.
    Unmatched text (surrounding prose) is replaced with [REDACTED] by the
    redact policy, preventing undetected PII passthrough.

    HONEST limitation (SP-07 docs): text_mask's built-in path masks only
    the 11 TIER-1 detectors. Names and dates embedded in free text are NOT
    separately detected; they are caught by the redact-of-unmatched default
    rather than by a name detector. This is documented in the HIPAA pack YAML.
    """

    _SSN_NOTE = "Patient SSN is 123-45-6789. Presented with chest pain."
    _NPI_NOTE = "No SSN on file. History of hypertension. NPI: 1234567893."
    _SEED = b"\xab" * 32

    def test_A3_ssn_span_in_clinical_notes_is_masked(self):
        """SSN detected in clinical_notes and masked; raw SSN absent from output."""
        from decoy_engine.transforms.text_mask import mask_cell

        result = mask_cell(
            self._SSN_NOTE,
            self._SEED,
            detector_ids=["ssn"],
            unmatched_span_policy="redact",
        )
        assert "123-45-6789" not in result, (
            f"Raw SSN '123-45-6789' must not appear in masked clinical_notes. Got: {result!r}"
        )

    def test_A3_unmatched_prose_is_redacted_not_passed_through(self):
        """Unmatched prose is replaced with the redact token, not passed through.

        policy=redact: after masking the SSN span, the remaining prose is also
        replaced with [REDACTED]. This prevents surrounding free-text (which
        may contain undetected PII like names) from leaking.
        """
        from decoy_engine.transforms.text_mask import mask_cell

        result = mask_cell(
            self._SSN_NOTE,
            self._SEED,
            detector_ids=["ssn"],
            unmatched_span_policy="redact",
        )
        # "Presented with chest pain." is unmatched prose; must be redacted.
        assert "Presented with chest pain" not in result, (
            f"Unmatched clinical prose must be redacted when unmatched_span_policy='redact'. "
            f"Got: {result!r}. This prevents undetected PII in prose from leaking."
        )

    def test_A3_output_contains_redacted_token(self):
        """Output contains at least one [REDACTED] token (from span or unmatched prose)."""
        from decoy_engine.transforms.text_mask import mask_cell

        result = mask_cell(
            self._SSN_NOTE,
            self._SEED,
            unmatched_span_policy="redact",
        )
        assert "[REDACTED]" in result, (
            f"Expected [REDACTED] tokens in masked output for redact policy. Got: {result!r}"
        )

    def test_A3_npi_span_in_clinical_notes_is_masked(self):
        """NPI span within clinical notes is masked (NPI is a TIER-1 detector)."""
        from decoy_engine.transforms.text_mask import mask_cell

        result = mask_cell(
            self._NPI_NOTE,
            self._SEED,
            detector_ids=["npi"],
            unmatched_span_policy="redact",
        )
        # The raw NPI value must not appear in the masked output.
        assert "1234567893" not in result, (
            f"Raw NPI '1234567893' must not appear in masked clinical_notes. Got: {result!r}"
        )

    def test_A3_empty_cell_is_handled(self):
        """Empty clinical_notes cells are handled without error."""
        from decoy_engine.transforms.text_mask import mask_cell

        result = mask_cell("", self._SEED, unmatched_span_policy="redact")
        assert isinstance(result, str), "mask_cell must return a string for empty input."

    def test_A3_full_claims_notes_have_no_raw_ssn(self):
        """All clinical_notes in the claims fixture have SSNs masked.

        Iterates the full fixture's clinical_notes column; asserts that no
        value in the masked output contains any of the raw SSN values that
        appear in the source data.
        """
        from decoy_engine.transforms.text_mask import mask_cell

        raw_ssns = ["123-45-6789", "987-65-4320"]
        for note in _CLAIMS_DATA["clinical_notes"]:
            masked = mask_cell(note, self._SEED, unmatched_span_policy="redact")
            for ssn in raw_ssns:
                assert ssn not in masked, (
                    f"Raw SSN {ssn!r} found in masked clinical_notes output: {masked!r}"
                )


# ── A4: ZIP Safe Harbor acceptance ────────────────────────────────────────────


class TestZipSafeHarborAcceptance:
    """A4: geo_generalize ZIP Safe Harbor cascade.

    Large ZIP3 prefix (981, Seattle area): in-dataset count does not reach
    20000 so zip5 level is skipped; prefix 981 is not restricted, so zip3
    is emitted. Restricted ZIP3 prefix (036, NH): in the HHS-restricted list,
    so the cascade must skip zip3 and emit state or suppress.
    Reference: 45 CFR 164.514(b)(2)(i)(B) HIPAA Safe Harbor geographic units.
    """

    def _config(self):
        from decoy_engine.transforms.geo_generalize import GeoGeneralizeConfig

        return GeoGeneralizeConfig(
            type="zip",
            cascade=["zip5", "zip3", "state", "suppress"],
            k_threshold=20000,
        )

    def test_A4_large_zip3_generalizes_to_zip3(self):
        """A ZIP3 prefix not in the HHS restricted list emits the 3-digit prefix.

        prefix 981 (Seattle WA) is not restricted; with k_threshold=20000 and
        only 3 rows in-dataset the zip5 level is not reached, so zip3 wins.
        """
        from decoy_engine.transforms.geo_generalize import cascade_zip_column

        df = pd.DataFrame({"zipcode": ["98101", "98102", "98103"]})
        result_df, evidence = cascade_zip_column(df, "zipcode", self._config())
        for val in result_df["zipcode"]:
            assert val == "981", (
                f"Non-restricted ZIP3 '981' should generalize to zip3 level; got {val!r}."
            )

    def test_A4_restricted_zip3_cascades_past_zip3(self):
        """A ZIP3 in the HHS restricted list (036) must not appear in output.

        prefix 036 is in the restricted set (population < 20,000 per 45 CFR
        164.514(b)(2)(i)(B)); the cascade skips zip3 and goes to state or suppress.
        """
        from decoy_engine.transforms.geo_generalize import cascade_zip_column

        df = pd.DataFrame({"zipcode": ["03601", "03602"]})
        result_df, evidence = cascade_zip_column(df, "zipcode", self._config())
        for val in result_df["zipcode"]:
            assert val != "036", (
                f"Restricted ZIP3 '036' must not appear in output; cascade must go past "
                f"zip3 (to state or suppress). Got {val!r}. Decisions: {evidence.decisions}"
            )

    def test_A4_cascade_evidence_records_per_row_decisions(self):
        """cascade_zip_column returns CascadeEvidence with one decision per row."""
        from decoy_engine.transforms.geo_generalize import cascade_zip_column

        df = pd.DataFrame({"zipcode": ["98101", "98102", "03601"]})
        _, evidence = cascade_zip_column(df, "zipcode", self._config())
        assert len(evidence.decisions) == 3, (
            f"CascadeEvidence must record one decision per row; got {evidence.decisions}"
        )
        # Row 2 (03601 -> prefix 036) must not be 'zip3' in decisions.
        assert evidence.decisions[2] != "zip3", (
            f"Row with restricted ZIP3 '036' must not record a zip3 decision; "
            f"decisions={evidence.decisions}"
        )

    def test_A4_k_threshold_matches_safe_harbor_default(self):
        """The HIPAA pack geo_generalize rule uses k_threshold=20000 (Safe Harbor)."""
        rule = _rule_for_detector("us_zip")
        assert rule is not None
        assert rule.params.get("k_threshold") == 20000, (
            f"geo_generalize rule must carry k_threshold=20000 matching the HIPAA Safe "
            f"Harbor population floor (45 CFR 164.514(b)(2)(i)(B)); got {rule.params}"
        )


# ── Honesty guards: verify the pack does NOT over-claim ───────────────────────


class TestHonestCoverageClaims:
    """Honesty guards: the pack must not claim capabilities it does not have.

    These assertions prevent documentation drift where YAML comments or the
    summary over-claim coverage not backed by shipped code.
    """

    def test_H5_loinc_not_in_field_rules(self):
        """LOINC code_set corpus is not shipped (SP-09 carry-forward).

        The HIPAA pack must NOT contain a field_rule referencing a 'loinc'
        detector because no such detector exists and the corpus is not shipped.
        Honest note: LOINC code_set is deferred to a later sprint.
        """
        pack = _hipaa_pack()
        loinc_rules = [r for r in pack.field_rules if "loinc" in r.detectors]
        assert not loinc_rules, (
            f"HIPAA pack must NOT carry a field_rule for 'loinc' (corpus not shipped). "
            f"Found: {loinc_rules}"
        )

    def test_H5_hcpcs_not_in_field_rules(self):
        """HCPCS columns have no registered STORM detector; pack cannot route them.

        The hcpcs corpus exists for manual code_set use, but no 'hcpcs' detector
        is registered in STORM. Users must configure code_set: hcpcs manually.
        """
        pack = _hipaa_pack()
        hcpcs_rules = [r for r in pack.field_rules if "hcpcs" in r.detectors]
        assert not hcpcs_rules, (
            f"HIPAA pack must NOT carry a field_rule for 'hcpcs' (no registered detector). "
            f"Found: {hcpcs_rules}"
        )

    def test_H5_clinical_notes_not_in_field_rules(self):
        """clinical_notes is not a registered STORM detector.

        Free-text columns must be configured with text_mask manually in the
        recipe YAML. The pack cannot auto-route them without a registered detector.
        """
        pack = _hipaa_pack()
        freetext_rules = [r for r in pack.field_rules if "clinical_notes" in r.detectors]
        assert not freetext_rules, (
            f"HIPAA pack must NOT carry a field_rule for 'clinical_notes' (no such "
            f"registered detector). Found: {freetext_rules}"
        )


# ── Pack-param-driven end-to-end tests ────────────────────────────────────────


class TestPackParamDriven:
    """Pack-param-driven end-to-end tests for the three SP-11 tightened detectors.

    These tests load the actual shipped HIPAA pack, extract rule.params for
    npi, icd10, and us_zip FROM THE LOADED PACK (not hand-built dicts), and
    drive those params through the strategy entry points:
      - NPI: fpe_encrypt_value with rule.params.get('checksum')
      - ICD-10: CodeSetConfig.from_dict(rule.params) -> apply_code_set
      - ZIP: GeoGeneralizeConfig.from_dict(rule.params) -> cascade_zip_column

    Teeth guarantee: if a key is dropped or renamed in hipaa.yaml, the test
    fails at apply time because rule.params no longer carries the expected key.
    Examples:
      - Rename 'checksum' -> 'chk' in hipaa.yaml: rule.params.get('checksum')
        returns None, fpe_encrypt_value skips checksum recomputation, and
        checksums.validate('npi', masked) fails -> TEST FAILS.
      - Rename 'chapter_preserve' -> 'chapter_pres': CodeSetConfig.from_dict
        uses cfg.get('chapter_preserve', False) = False, chapter preservation
        is disabled, I-codes may map to any chapter -> TEST FAILS.
      - Rename 'cascade' -> 'cascade_levels': validate_geo_generalize_config
        raises PlanCompileError because 'cascade' is a required key -> TEST FAILS.
      - Rename 'type' -> 'typ': same PlanCompileError on the 'type' key -> TEST FAILS.
    """

    _JOB_SEED = b"\xca\xfe" * 16
    _FPE_KEY = bytes(range(32))
    _FPE_TWEAK = b"npi"

    def _pack(self):
        return {d.id: d for d in load_disguises()}["hipaa"]

    def _rule(self, detector_id: str):
        pack = self._pack()
        return next(r for r in pack.field_rules if detector_id in r.detectors)

    # NPI: fpe with checksum from rule.params

    def test_P1_npi_pack_params_drive_valid_npi_output(self):
        """NPI rule.params from the loaded pack drive fpe+checksum -> valid NPI.

        Uses npi_rule.params.get('checksum') (not the hard-coded string 'npi').
        If 'checksum' is renamed or dropped in hipaa.yaml, params.get('checksum')
        returns None, fpe_encrypt_value skips recomputation, and
        checksums.validate fails -- the test has teeth.
        """
        from decoy_engine.transforms.fpe import fpe_encrypt_value

        npi_rule = self._rule("npi")
        checksum = npi_rule.params.get("checksum")  # from YAML, not hand-built

        for npi in ["1234567893", "1679576722", "1000000004"]:
            masked = fpe_encrypt_value(
                npi, self._FPE_KEY, "0123456789", self._FPE_TWEAK, checksum=checksum
            )
            assert checksums.validate("npi", masked), (
                f"NPI {npi!r} -> {masked!r}: checksums.validate failed. "
                f"Loaded npi_rule.params={npi_rule.params!r}. "
                f"Dropping or renaming 'checksum' in hipaa.yaml causes this failure."
            )

    def test_P1_npi_pack_params_mask_is_fpe_strategy(self):
        """Confirm the NPI rule uses mask='fpe' (not truncate or faker).

        Belt-and-suspenders: the structural test (TestHipaaPackStructure.test_H1)
        already checks this. Including here so the pack-param test class is
        self-contained for readers.
        """
        npi_rule = self._rule("npi")
        assert npi_rule.mask == "fpe", (
            f"NPI rule.mask must be 'fpe'; got {npi_rule.mask!r}. "
            f"A strategy rename in hipaa.yaml breaks this assertion."
        )

    # ICD-10: CodeSetConfig.from_dict driven by rule.params

    def test_P2_icd10_pack_params_drive_chapter_preserved_output(self):
        """ICD-10 rule.params from the loaded pack drive CodeSetConfig.from_dict -> chapter preserved.

        Uses icd_rule.params (not a hand-built dict) with CodeSetConfig.from_dict.
        If 'chapter_preserve' is renamed or dropped in hipaa.yaml, from_dict
        uses cfg.get('chapter_preserve', False) = False, chapter preservation
        is disabled, and I-codes may map outside chapter I -- test has teeth.
        """
        from decoy_engine.transforms.code_set import CodeSetConfig, apply_code_set

        icd_rule = self._rule("icd10")
        cfg = CodeSetConfig.from_dict(icd_rule.params)  # params straight from YAML

        for code in ["I21.9", "I25.10", "I50.9"]:
            masked = apply_code_set(code, cfg, mode="mask", job_seed=self._JOB_SEED)
            assert masked.startswith("I"), (
                f"Cardiovascular I-code {code!r} -> {masked!r}: chapter not preserved. "
                f"Loaded icd_rule.params={icd_rule.params!r}. "
                f"Dropping 'chapter_preserve' from hipaa.yaml disables preservation "
                f"and causes cross-chapter output, failing this assertion."
            )

    def test_P2_icd10_pack_params_emit_real_corpus_code(self):
        """ICD-10 pack-driven output is a real corpus code, not a truncation."""
        from decoy_engine.transforms.code_set import CodeSetConfig, apply_code_set, load_corpus

        icd_rule = self._rule("icd10")
        cfg = CodeSetConfig.from_dict(icd_rule.params)
        corpus_codes = {row["code"] for row in load_corpus("icd10")}

        for code in ["I21.9", "I25.10", "I50.9"]:
            masked = apply_code_set(code, cfg, mode="mask", job_seed=self._JOB_SEED)
            assert masked in corpus_codes, (
                f"ICD-10 output {masked!r} (from {code!r}) is not in the shipped corpus. "
                f"Loaded icd_rule.params={icd_rule.params!r}."
            )

    # ZIP: GeoGeneralizeConfig.from_dict driven by rule.params

    def test_P3_zip_pack_params_drive_restricted_prefix_suppressed(self):
        """ZIP rule.params from the loaded pack drive GeoGeneralizeConfig.from_dict -> restricted suppressed.

        Uses zip_rule.params (not a hand-built dict) with GeoGeneralizeConfig.from_dict.
        If 'cascade' is renamed or dropped in hipaa.yaml, from_dict raises
        PlanCompileError (cascade is a required key) -- test has teeth.
        If 'type' is renamed, from_dict also raises PlanCompileError.
        """
        from decoy_engine.transforms.geo_generalize import GeoGeneralizeConfig, cascade_zip_column

        zip_rule = self._rule("us_zip")
        cfg = GeoGeneralizeConfig.from_dict(zip_rule.params)  # params straight from YAML

        df = pd.DataFrame({"zipcode": ["03601", "03602"]})
        result_df, evidence = cascade_zip_column(df, "zipcode", cfg)

        for val in result_df["zipcode"]:
            assert val != "036", (
                f"Restricted ZIP3 '036' must not appear in output; got {val!r}. "
                f"Loaded zip_rule.params={zip_rule.params!r}. "
                f"Dropping 'cascade' from hipaa.yaml raises PlanCompileError; "
                f"dropping k_threshold falls back to the HIPAA default (20000) "
                f"which also suppresses restricted prefixes."
            )

    def test_P3_zip_pack_params_large_prefix_emits_zip3(self):
        """ZIP rule.params from the loaded pack: non-restricted ZIP3 emits zip3 output."""
        from decoy_engine.transforms.geo_generalize import GeoGeneralizeConfig, cascade_zip_column

        zip_rule = self._rule("us_zip")
        cfg = GeoGeneralizeConfig.from_dict(zip_rule.params)

        df = pd.DataFrame({"zipcode": ["98101", "98102", "98103"]})
        result_df, _ = cascade_zip_column(df, "zipcode", cfg)

        for val in result_df["zipcode"]:
            assert val == "981", (
                f"Non-restricted ZIP3 '981' should generalize to '981'; got {val!r}. "
                f"Loaded zip_rule.params={zip_rule.params!r}."
            )

    def test_P3_zip_pack_params_k_threshold_is_safe_harbor_floor(self):
        """The k_threshold extracted from rule.params is 20000 (HIPAA Safe Harbor floor).

        This asserts the YAML value feeds through correctly. If k_threshold is
        renamed in hipaa.yaml, GeoGeneralizeConfig falls back to the HIPAA default
        (20000) -- the test still passes, but the explicit YAML value is no longer
        the source of truth. The renamed-cascade / renamed-type tests above carry
        the primary teeth for this detector.
        """
        zip_rule = self._rule("us_zip")
        k = zip_rule.params.get("k_threshold")
        assert k == 20000, (
            f"hipaa.yaml zip rule must carry k_threshold=20000; got {k!r}. "
            f"Full params: {zip_rule.params!r}."
        )
