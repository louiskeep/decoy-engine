"""FPE charset-coverage leak tests for shipped disguise packs.

A fixture-leak sweep (platform side, 2026-06-29) identified the FPE
passthrough class: the engine's FPE with preserve_separators=True (the
default) leaves characters OUTSIDE the declared charset in place, verbatim,
in the output.  That means a rule with charset='alphanum' (= 0-9 + a-z,
NO uppercase A-Z) will emit any uppercase letter from the source value
unchanged -- the letter is present in the masked output in the clear.

The only shipped rule that under-covers the value alphabet is:
  hipaa.yaml  mrn -> fpe charset: alphanum

Medical Record Numbers are institution-specific identifiers (no universal
standard).  Formats range from purely numeric (e.g. "0001234567" in many
Epic deployments) to uppercase-containing alphanumeric (e.g. "MRN12345A",
"A1234567890B").  Because the value alphabet is unknown and uppercase
letters are plausible, the safe charset is ALPHANUM (0-9 + A-Z + a-z),
which covers the full alphanumeric superset: no character ever passes
through for any alphanumeric identifier value.

These tests:
  1. BITE under the old alphanum charset: prove uppercase letters leak.
  2. PASS under the new ALPHANUM charset: prove no uppercase letter leaks.
  3. Assert the shipped HIPAA pack rule carries charset=ALPHANUM (structural).
  4. Drive the HIPAA pack's actual mrn rule params through fpe_encrypt_value
     so renaming a YAML key causes immediate test failure (pack-param teeth).

References: fpe.py _CHARSETS definition; _fpe_value preserve_separators path.
"""

from __future__ import annotations

import pytest

from decoy_engine.transforms.fpe import _CHARSETS, fpe_encrypt_value

# Stable key and tweak; value chosen to exhibit a real Feistel permutation.
_FPE_KEY = bytes(range(32))
_MRN_TWEAK = b"mrn"

# Representative MRN values that contain uppercase letters.
# These values are realistic for healthcare institutions whose MRN schemes
# include uppercase alpha characters (no universal standard; HIPAA Safe Harbor
# H identifies MRNs as identifiers but does not specify a format).
_UPPERCASE_MRNS = [
    "MRN12345A",  # typical uppercase prefix + digit body + suffix
    "A1234567890B",  # uppercase bookends around a digit run
    "ABC123",  # short all-cap alpha prefix
    "PT00012345X",  # patient-id style with uppercase letters
]

# Charset strings (resolved from the name table to avoid depending on the
# name resolving path and make the bite / safe contrast explicit).
_CHARSET_ALPHANUM = _CHARSETS["alphanum"]  # 0-9 + a-z  (the old, under-covering charset)
_CHARSET_ALPHANUM_UPPER = _CHARSETS["ALPHANUM"]  # 0-9 + A-Z + a-z  (the safe charset)


# ── L1: BITE -- prove the old alphanum charset leaks uppercase characters ──────


class TestOldAlphanumLeaks:
    """Prove the under-covering charset ('alphanum') leaks uppercase letters.

    With preserve_separators=True (the default), FPE leaves characters outside
    the declared charset in place verbatim.  alphanum = 0-9+a-z; any uppercase
    letter in the value is not in the charset and therefore passes through in
    the clear in the masked output.

    These tests are expected to PASS (they demonstrate the leak).  They
    document the pre-fix behaviour so it is unambiguous that the fix is needed.
    """

    @pytest.mark.parametrize("mrn", _UPPERCASE_MRNS)
    def test_L1_alphanum_leaks_uppercase_char_in_place(self, mrn: str) -> None:
        """With charset=alphanum an uppercase letter in an MRN passes through verbatim.

        For each uppercase character in the source MRN, the masked output
        must contain that exact character at the same position -- the leak.
        If this assertion ever fails it means FPE behaviour changed so that
        out-of-charset chars are no longer passed through; the leak class
        would no longer exist and the fix would be moot.
        """
        masked = fpe_encrypt_value(
            mrn, _FPE_KEY, _CHARSET_ALPHANUM, _MRN_TWEAK, preserve_separators=True
        )
        upper_positions = [i for i, ch in enumerate(mrn) if ch.isupper()]
        assert upper_positions, (
            f"Test value {mrn!r} has no uppercase letters; choose a better fixture."
        )
        for i in upper_positions:
            assert masked[i] == mrn[i], (
                f"EXPECTED LEAK: position {i} of masked MRN {masked!r} "
                f"(from {mrn!r}) should equal the source character {mrn[i]!r} "
                f"when charset=alphanum (uppercase not in charset, passed through verbatim). "
                f"If this fails, the FPE passthrough behaviour has changed."
            )

    def test_L1_at_least_one_sample_leaks_uppercase(self) -> None:
        """Summary assertion: at least one _UPPERCASE_MRNS sample has a leak under alphanum."""
        leaked_any = False
        for mrn in _UPPERCASE_MRNS:
            masked = fpe_encrypt_value(
                mrn, _FPE_KEY, _CHARSET_ALPHANUM, _MRN_TWEAK, preserve_separators=True
            )
            upper_positions = [i for i, ch in enumerate(mrn) if ch.isupper()]
            if any(masked[i] == mrn[i] for i in upper_positions):
                leaked_any = True
                break
        assert leaked_any, (
            "Expected at least one MRN sample to leak an uppercase letter under "
            "charset=alphanum.  FPE passthrough semantics may have changed."
        )


# ── L2: NO-LEAK -- prove the new ALPHANUM charset does not leak uppercase ──────


class TestNewAlphanumCoversUppercase:
    """Prove the covering charset ('ALPHANUM') does not leak uppercase letters.

    With charset=ALPHANUM (0-9+A-Z+a-z), every alphanumeric character (including
    uppercase) is inside the charset.  The Feistel permutation encrypts ALL
    characters; none are passed through verbatim.  Therefore no source character
    appears at the same position in the output.

    These tests PASS after the fix and document the corrected behaviour.
    Note: the permutation is a bijection, so for a given (key, tweak, charset)
    the probability of a character mapping to itself is 1/|charset| = 1/62 per
    position; a fixed point at any single position is possible by chance.
    The tests check across multiple values; the probability that ALL positions
    across all values are fixed points is astronomically small.
    """

    @pytest.mark.parametrize("mrn", _UPPERCASE_MRNS)
    def test_L2_ALPHANUM_encrypts_all_chars_no_verbatim_passthrough(self, mrn: str) -> None:
        """With charset=ALPHANUM, uppercase chars are encrypted (not passed through).

        The masked output must differ from the source in at least one position
        that holds an uppercase letter.  A perfect fixed-point permutation
        (output == input) would also be a bug in the Feistel implementation;
        the test rejects that too.

        If by astronomical chance the permutation IS a fixed point for a given
        (key, tweak, value), this test would incorrectly flag -- but that case
        is computationally implausible for a 32-byte key.
        """
        masked = fpe_encrypt_value(
            mrn, _FPE_KEY, _CHARSET_ALPHANUM_UPPER, _MRN_TWEAK, preserve_separators=True
        )
        # All uppercase positions in the source must be encrypted (not passed through).
        # A character in ALPHANUM is permuted, not left in place -- so the masked
        # value at each position is the PERMUTED character, which may differ from source.
        # The test checks the output is not simply the input (total identity is impossible
        # for a non-trivial permutation).
        assert masked != mrn, (
            f"ALPHANUM-masked MRN {masked!r} must not equal source {mrn!r}. "
            f"FPE must permute at least one character."
        )
        # And: no uppercase source character is present verbatim at its own position
        # in the way the alphanum leak works (passthrough at the same position).
        # We assert that the masked result over ALPHANUM is different to the alphanum
        # masked result (which leaks) -- proving ALPHANUM adds real encryption here.
        masked_leaking = fpe_encrypt_value(
            mrn, _FPE_KEY, _CHARSET_ALPHANUM, _MRN_TWEAK, preserve_separators=True
        )
        # Under alphanum, uppercase letters pass through; under ALPHANUM they are permuted.
        # The two outputs must therefore differ (at the uppercase positions).
        upper_positions = [i for i, ch in enumerate(mrn) if ch.isupper()]
        if upper_positions:
            assert any(masked[i] != masked_leaking[i] for i in upper_positions), (
                f"ALPHANUM output {masked!r} and alphanum output {masked_leaking!r} must "
                f"differ at uppercase positions {upper_positions} for MRN {mrn!r}. "
                f"ALPHANUM encrypts uppercase characters; alphanum passes them through."
            )

    def test_L2_alphanum_vs_ALPHANUM_differ_at_uppercase_positions_summary(self) -> None:
        """Summary: over all test MRNs, ALPHANUM and alphanum diverge at uppercase positions.

        This is the clearest statement of the fix: the two charsets produce
        different outputs at exactly the positions that hold uppercase letters.
        alphanum leaks those positions verbatim; ALPHANUM encrypts them.
        """
        for mrn in _UPPERCASE_MRNS:
            masked_leaking = fpe_encrypt_value(
                mrn, _FPE_KEY, _CHARSET_ALPHANUM, _MRN_TWEAK, preserve_separators=True
            )
            masked_safe = fpe_encrypt_value(
                mrn, _FPE_KEY, _CHARSET_ALPHANUM_UPPER, _MRN_TWEAK, preserve_separators=True
            )
            upper_positions = [i for i, ch in enumerate(mrn) if ch.isupper()]
            for i in upper_positions:
                # Under alphanum, leaked char == source char.
                assert masked_leaking[i] == mrn[i], (
                    f"alphanum should leak position {i} of {mrn!r} (src={mrn[i]!r}, "
                    f"got masked_leaking[{i}]={masked_leaking[i]!r})"
                )
                # Under ALPHANUM, char is permuted (may differ from source).
                # The critical difference: ALPHANUM output is NOT constrained to
                # equal the source at uppercase positions -- it may differ or
                # coincidentally equal it (1/62 chance per position).
                # The real test is that the two masked strings differ here.
                assert masked_safe[i] != masked_leaking[i], (
                    f"ALPHANUM and alphanum must produce different characters at "
                    f"uppercase position {i} of MRN {mrn!r}: "
                    f"alphanum passes source char {mrn[i]!r} through; "
                    f"ALPHANUM permutes the character. "
                    f"If they match it means ALPHANUM happened to map to the "
                    f"same char as alphanum passthrough, which is impossible "
                    f"(alphanum leaves chars unchanged; ALPHANUM permutes them "
                    f"using a different effective alphabet)."
                )


# ── P1: Pack structural assertion -- HIPAA mrn rule must use ALPHANUM ──────────


class TestHipaaPackMrnCharset:
    """Structural: the shipped HIPAA pack mrn rule must carry charset=ALPHANUM.

    These tests fail before the YAML fix and pass after.  They document the
    invariant that the mrn rule's charset covers the full alphanumeric
    superset (0-9+A-Z+a-z), so no character in any plausible MRN value
    passes through verbatim.
    """

    def _hipaa_mrn_rule(self):
        from decoy_engine.disguises import load_disguises

        packs = {d.id: d for d in load_disguises()}
        hipaa = packs["hipaa"]
        for rule in hipaa.field_rules:
            if "mrn" in rule.detectors:
                return rule
        return None

    def test_P1_mrn_rule_exists(self) -> None:
        """HIPAA pack must carry a field_rule for the 'mrn' detector."""
        rule = self._hipaa_mrn_rule()
        assert rule is not None, (
            "HIPAA pack has no field_rule for the 'mrn' detector. "
            "Safe Harbor section H (medical record numbers) requires a masking rule."
        )

    def test_P1_mrn_rule_uses_fpe(self) -> None:
        """HIPAA mrn rule must use the 'fpe' strategy."""
        rule = self._hipaa_mrn_rule()
        assert rule is not None
        assert rule.mask == "fpe", (
            f"HIPAA mrn rule must use mask='fpe'; got {rule.mask!r}. "
            f"FPE preserves format and FK joins; other strategies lose that property."
        )

    def test_P1_mrn_rule_charset_is_ALPHANUM(self) -> None:
        """HIPAA mrn rule must carry charset='ALPHANUM', not 'alphanum'.

        MRNs are institution-specific identifiers (no universal standard;
        45 CFR 164.514(b)(2) Safe Harbor section H).  Formats range from
        purely numeric to uppercase-containing alphanumeric.  charset=alphanum
        (0-9+a-z) does not include uppercase A-Z; any uppercase letter in
        the source MRN passes through verbatim (FPE preserve_separators
        passthrough on out-of-charset characters).  charset=ALPHANUM (0-9+A-Z+a-z)
        covers the full alphanumeric superset so no character ever passes through.
        """
        rule = self._hipaa_mrn_rule()
        assert rule is not None
        charset = rule.params.get("charset")
        assert charset == "ALPHANUM", (
            f"HIPAA mrn rule must carry charset='ALPHANUM' to cover the full "
            f"alphanumeric superset (0-9+A-Z+a-z).  Got charset={charset!r}. "
            f"charset='alphanum' (0-9+a-z, no uppercase) leaks any uppercase "
            f"letter present in the source MRN value verbatim in the masked output."
        )


# ── P2: Pack-param-driven end-to-end: mrn rule params -> no uppercase leak ────


class TestHipaaPackMrnEndToEnd:
    """Pack-param-driven: HIPAA mrn rule.params from the loaded pack -> no leak.

    Drives the actual YAML params through fpe_encrypt_value the same way
    the execution engine does.  If 'charset' is renamed or changed in
    hipaa.yaml, the test fails because the wrong charset produces a leak.
    Teeth guarantee:
      - Rename 'ALPHANUM' -> 'alphanum' in hipaa.yaml: charset becomes
        alphanum, uppercase chars pass through, L1-style assertions fire -> FAIL.
      - Drop 'charset' key: charset defaults to 'digits', which also fails
        to cover uppercase chars -> FAIL.
    """

    _KEY = bytes(range(32))
    _TWEAK = b"mrn"

    def _mrn_rule_params(self):
        from decoy_engine.disguises import load_disguises

        packs = {d.id: d for d in load_disguises()}
        hipaa = packs["hipaa"]
        for rule in hipaa.field_rules:
            if "mrn" in rule.detectors:
                return rule.params
        raise AssertionError("No mrn rule found in HIPAA pack")

    @pytest.mark.parametrize("mrn", _UPPERCASE_MRNS)
    def test_P2_pack_params_drive_no_uppercase_passthrough(self, mrn: str) -> None:
        """mrn rule.params from loaded HIPAA pack -> uppercase chars are encrypted.

        Loads charset from rule.params (not hard-coded), resolves it via
        _CHARSETS, then asserts that no uppercase source character passes
        through verbatim at the same position.

        Teeth: if hipaa.yaml mrn charset is 'alphanum', the resolved charset
        is 0-9+a-z, uppercase chars pass through, and the assertion fires.
        """
        params = self._mrn_rule_params()
        charset_name = params.get("charset", "digits")
        resolved_charset = _CHARSETS.get(charset_name, charset_name)

        masked = fpe_encrypt_value(
            mrn, self._KEY, resolved_charset, self._TWEAK, preserve_separators=True
        )
        upper_positions = [i for i, ch in enumerate(mrn) if ch.isupper()]
        for i in upper_positions:
            assert masked[i] != mrn[i], (
                f"Uppercase char {mrn[i]!r} at position {i} of MRN {mrn!r} "
                f"passed through verbatim in masked output {masked!r}. "
                f"Pack mrn rule params={params!r}; resolved charset={charset_name!r}. "
                f"The charset does not cover uppercase letters; widen to ALPHANUM."
            )
