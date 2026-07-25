"""Property + metamorphic invariants for the FPE (format-preserving
encryption) mask strategy: the highest-blast-radius crypto module in the
engine, held to the 100% mutation bar (see the crypto/RI mandate in
`docs/quality/module-test-quality-playbook.md`).

The existing covering suite (`tests/codspeed/test_fpe_transform.py`,
`tests/unit/plan/test_check_fpe_charset.py`, `tests/unit/transforms/
test_fpe_roundtrip.py`, `test_fpe_checksum_validity.py`,
`test_fpe_remap_orphan_charset.py`, and the Hypothesis round-trip in
`tests/property/test_mask_invariants.py::test_fpe_decrypt_inverts_encrypt`)
already example-tests and property-tests format preservation + plain-mode
invertibility. This module LAYERS ON rather than repeats: it adds the
invariants those suites don't cover as properties (determinism as a
first-class assertion, key/tweak sensitivity, out-of-charset domain
validation, the empty/single-char boundary, and checksum/Luhn round-trips
over RANDOM bodies instead of hand-picked examples), and it broadens the
charset domain to include synthesized custom charsets, not just the 5 named
ones.

Invariant sources (cite-the-source-pattern, per repo CLAUDE.md):

- FORMAT PRESERVATION + INVERTIBILITY: `transforms/fpe.py` module docstring
  ("Replaces each string value with another string of the same length over
  the same character set... The Feistel construction is a bijection
  regardless of the round function"). Feistel: Horst Feistel, IBM, 1973.
  HMAC: RFC 2104. This is the KILLER metamorphic property -- a bijection
  that isn't actually invertible is a broken cipher, and almost any mutant
  that corrupts the round arithmetic breaks the round trip.
- DETERMINISM: `FPEStrategy` docstring ("Same input + same key -> same
  output (keyed determinism)"), the same keyed-determinism contract
  `HashStrategy` and `DateShiftStrategy` share.
- KEY/TWEAK SENSITIVITY: implied by the cipher being keyed at all (a PRF
  that ignores its key is not a PRF); NIST SP 800-38G (FF1) sec. 4 makes
  the tweak part of the encryption's identity for the same reason. Domain
  is gated to >= 1,000,000 possible values (radix^length; the same bound
  FF1 sec. 5.2 sets as its OWN minimum-domain requirement) so a
  coincidental collision across two random keys is negligible.
- DOMAIN VALIDATION: `FpeUnencryptableError`'s docstring in
  `decoy_engine/errors.py` (DE-01 cluster-C, 2026-07-14) -- the two fail
  types (all-out-of-charset always closed; any-out-of-charset closed under
  `preserve_separators=False`).
- BOUNDARY (this module is NOT NIST FF1; it has its own documented minimums,
  not FF1's minlen=2/radix^minlen>=1e6 rule -- see the module's "Design
  note" at the top): empty string is an explicit documented passthrough
  (`_fpe_value`/`_fpe_pure_value` docstrings, "Empty-string preserve");
  length 1 uses the dedicated `_single_char_shift` rotation (QA-10 F2,
  2026-06-01, "uniform alphabet rotation; trivially bijective"); length < 2
  under `validate_luhn=True` silently falls back to the plain permutation
  (the `len(s) >= 2` guard in `_fpe_pure_value`) since there's no separate
  check-digit position to reserve.
- NO-OP LEAKAGE: a mask that returns its input unchanged provides zero
  protection; gated to the same negligible-collision domain as key/tweak
  sensitivity.
- LUHN: Hans Peter Luhn, US Patent 2,950,048 (1954), public-domain checksum.

Run:  pytest tests/property/test_fpe_invariants.py -q
"""

from __future__ import annotations

import string

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import decoy_engine.checksums as checksums
from decoy_engine.errors import FpeUnencryptableError
from decoy_engine.transforms.fpe import (
    _CHARSETS,
    _luhn_check_digit,
    fpe_decrypt_value,
    fpe_encrypt_value,
)

# Match the pilot's audit profile: more examples than the 100-example
# default, no deadline (8-round HMAC-SHA256 Feistel is cheap but Hypothesis
# shrinking can trip the 200ms wall), and print_blob so a counterexample is
# replayable.
settings.register_profile(
    "audit",
    max_examples=300,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("audit")

_NAMED_CHARSETS = list(_CHARSETS.values())
_CUSTOM_POOL = string.ascii_letters + string.digits + "!@#$%^&*_-+="
# A pool of characters unlikely to collide with a drawn charset; "★" is
# a backstop that can never be in an ASCII-only _CHARSETS/_CUSTOM_POOL charset.
_POISON_BASE = "()~`+=[]{}|;:'\",.<>?/\\ !@#$%^&*_-"
_POISON_BACKSTOP = "★"

_KEYS = st.binary(min_size=8, max_size=32)
_TWEAKS = st.binary(min_size=1, max_size=32)


@st.composite
def _charset(draw: st.DrawFn) -> str:
    """One of the engine's 5 named charsets, or a synthesized custom one
    (2-20 distinct characters) -- the two shapes `FPEStrategy.apply`
    resolves a `charset:` config value to (`_CHARSETS.get(spec, spec)`)."""
    if draw(st.booleans()):
        return draw(st.sampled_from(_NAMED_CHARSETS))
    size = draw(st.integers(min_value=2, max_value=20))
    chars = draw(st.lists(st.sampled_from(_CUSTOM_POOL), unique=True, min_size=size, max_size=size))
    return "".join(chars)


@st.composite
def _charset_and_value(draw: st.DrawFn, min_len: int = 0, max_len: int = 24) -> tuple[str, str]:
    """A charset plus a value made ENTIRELY of that charset's characters."""
    cs = draw(_charset())
    n = draw(st.integers(min_value=min_len, max_value=max_len))
    value = "".join(draw(st.lists(st.sampled_from(cs), min_size=n, max_size=n)))
    return cs, value


@st.composite
def _charset_and_value_nonvacuous(draw: st.DrawFn, min_domain: int = 1_000_000) -> tuple[str, str]:
    """A charset plus a value whose domain size (radix^length) clears
    `min_domain`, so a COINCIDENTAL collision across two independently
    generated keys/tweaks is negligible (< 1e-6). Mirrors FF1's own
    minimum-domain floor (NIST SP 800-38G sec. 5.2: radix^minlen >=
    1,000,000), even though this module is not FF1."""
    cs = draw(_charset())
    r = len(cs)
    n = 1
    while r**n < min_domain and n < 40:
        n += 1
    value = "".join(draw(st.lists(st.sampled_from(cs), min_size=n, max_size=n)))
    return cs, value


def _poison_candidates(cs: str) -> list[str]:
    candidates = [c for c in _POISON_BASE if c not in cs]
    return candidates or [_POISON_BACKSTOP]


@st.composite
def _charset_with_poison(draw: st.DrawFn) -> tuple[str, str]:
    """A charset plus ONE character guaranteed not in it."""
    cs = draw(_charset())
    return cs, draw(st.sampled_from(_poison_candidates(cs)))


@st.composite
def _all_out_of_charset_value(draw: st.DrawFn) -> tuple[str, str]:
    """A charset plus a non-empty value made ENTIRELY of characters outside it."""
    cs = draw(_charset())
    candidates = _poison_candidates(cs)
    n = draw(st.integers(min_value=1, max_value=10))
    value = "".join(draw(st.lists(st.sampled_from(candidates), min_size=n, max_size=n)))
    return cs, value


@st.composite
def _charset_value_with_separators(draw: st.DrawFn) -> tuple[str, str]:
    """A charset plus a value interleaving in-charset characters with
    out-of-charset "separator" characters at random positions, exercising
    `preserve_separators=True`'s partial-content contract."""
    cs = draw(_charset())
    sep_candidates = _poison_candidates(cs)
    n_body = draw(st.integers(min_value=0, max_value=12))
    body = draw(st.lists(st.sampled_from(cs), min_size=n_body, max_size=n_body))
    n_sep = draw(st.integers(min_value=0, max_value=6))
    seps = draw(st.lists(st.sampled_from(sep_candidates), min_size=n_sep, max_size=n_sep))
    chars = list(body)
    for s in seps:
        pos = draw(st.integers(min_value=0, max_value=len(chars)))
        chars.insert(pos, s)
    return cs, "".join(chars)


# --------------------------------------------------------------------------
# Format preservation + invertibility (the killer metamorphic property)
# --------------------------------------------------------------------------


@given(_charset_and_value(min_len=0, max_len=24), _KEYS, _TWEAKS)
def test_format_preservation_length_and_alphabet(data, key, tweak) -> None:
    """Module docstring: 'Replaces each string value with another string of
    the same length over the same character set.'"""
    cs, val = data
    enc = fpe_encrypt_value(val, key, cs, tweak)
    assert len(enc) == len(val)
    assert all(ch in cs for ch in enc)


@given(_charset_and_value(min_len=0, max_len=24), _KEYS, _TWEAKS)
def test_invertibility_decrypt_undoes_encrypt(data, key, tweak) -> None:
    """The killer property: `fpe_decrypt_value(fpe_encrypt_value(x)) == x`
    for every valid input, over a broader charset domain (including
    synthesized custom charsets) than the existing example-based suite.
    A Feistel network is a bijection by construction (module docstring);
    almost any mutant to the round arithmetic breaks this."""
    cs, val = data
    enc = fpe_encrypt_value(val, key, cs, tweak)
    assert fpe_decrypt_value(enc, key, cs, tweak) == val


@given(_charset_value_with_separators(), _KEYS, _TWEAKS)
def test_invertibility_holds_with_separators_preserved_in_place(data, key, tweak) -> None:
    """`preserve_separators=True`'s contract: out-of-charset characters stay
    at their original position, in-charset characters are permuted, and the
    whole thing still round-trips. Property-generalizes the hand-picked
    `"123-45-6789"` example in `test_fpe_roundtrip.py` to random charsets,
    random separator characters, and random interleavings."""
    cs, val = data
    assume(any(ch in cs for ch in val))  # exclude the all-separator case (tested separately)
    enc = fpe_encrypt_value(val, key, cs, tweak, preserve_separators=True)
    assert len(enc) == len(val)
    for src_ch, enc_ch in zip(val, enc, strict=True):
        if src_ch not in cs:
            assert enc_ch == src_ch
    assert fpe_decrypt_value(enc, key, cs, tweak, preserve_separators=True) == val


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


@given(_charset_and_value(min_len=0, max_len=24), _KEYS, _TWEAKS, st.booleans())
def test_determinism_same_key_and_tweak_same_output(data, key, tweak, preserve_sep) -> None:
    """`FPEStrategy` docstring: 'Same input + same key -> same output
    (keyed determinism).' Encrypting the same value twice under the same
    (key, charset, tweak, config) must be byte-identical -- this is what
    makes cross-run/cross-process fingerprints stable."""
    cs, val = data
    a = fpe_encrypt_value(val, key, cs, tweak, preserve_separators=preserve_sep)
    b = fpe_encrypt_value(val, key, cs, tweak, preserve_separators=preserve_sep)
    assert a == b


# --------------------------------------------------------------------------
# Key / tweak sensitivity (no key-independence leak)
# --------------------------------------------------------------------------


@given(_charset_and_value_nonvacuous(), _KEYS, _KEYS, _TWEAKS)
def test_different_key_changes_ciphertext(data, key_a, key_b, tweak) -> None:
    """A cipher whose output does not depend on the key is not keyed at
    all -- that would mean anyone (not just the key holder) could predict
    the mapping. Domain-gated to >= 1e6 possible values so a coincidental
    match between two independent random keys is negligible."""
    assume(key_a != key_b)
    cs, val = data
    enc_a = fpe_encrypt_value(val, key_a, cs, tweak)
    enc_b = fpe_encrypt_value(val, key_b, cs, tweak)
    assert enc_a != enc_b


@given(_charset_and_value_nonvacuous(), _KEYS, _TWEAKS, _TWEAKS)
def test_different_tweak_changes_ciphertext(data, key, tweak_a, tweak_b) -> None:
    """NIST SP 800-38G sec. 4 treats the tweak as part of the encryption's
    identity (two different tweaks under the same key must not collapse to
    the same permutation, or per-column tweaking -- this engine's
    `fpe_join_group`/column-name tweak -- would leak cross-column
    correlations). Domain-gated as above."""
    assume(tweak_a != tweak_b)
    cs, val = data
    enc_a = fpe_encrypt_value(val, key, cs, tweak_a)
    enc_b = fpe_encrypt_value(val, key, cs, tweak_b)
    assert enc_a != enc_b


# --------------------------------------------------------------------------
# No-op leakage
# --------------------------------------------------------------------------


@given(_charset_and_value_nonvacuous(), _KEYS, _TWEAKS)
def test_no_op_leakage_ciphertext_differs_from_plaintext(data, key, tweak) -> None:
    """A mask strategy that returns its input unchanged protects nothing.
    Domain-gated to >= 1e6 possible values so a coincidental Feistel fixed
    point is negligible (this is NOT true at tiny domains, e.g. a 1-char
    value over a 2-char charset has a 1-in-2 chance of a fixed rotation --
    see `test_single_character_...` below, which does not assert this)."""
    cs, val = data
    enc = fpe_encrypt_value(val, key, cs, tweak)
    assert enc != val


# --------------------------------------------------------------------------
# Domain validation
# --------------------------------------------------------------------------


@given(_charset_with_poison(), st.integers(min_value=0, max_value=10), _KEYS, _TWEAKS, st.data())
def test_out_of_charset_character_rejected_without_separator_preservation(
    cs_poison, body_len, key, tweak, data
) -> None:
    """`FpeUnencryptableError` (DE-01 cluster-C): under
    `preserve_separators=False`, ANY out-of-charset character fails closed
    (the pre-fix path silently returned the value unchanged -- a cleartext
    leak). One poisoned position in an otherwise-valid random body is
    enough to trip it."""
    cs, poison = cs_poison
    body = "".join(data.draw(st.lists(st.sampled_from(cs), min_size=body_len, max_size=body_len)))
    pos = data.draw(st.integers(min_value=0, max_value=len(body)))
    val = body[:pos] + poison + body[pos:]
    with pytest.raises(FpeUnencryptableError) as ei:
        fpe_encrypt_value(val, key, cs, tweak, preserve_separators=False)
    assert ei.value.code == "fpe.unencryptable"
    assert ei.value.value == val


@given(_all_out_of_charset_value(), _KEYS, _TWEAKS)
def test_all_out_of_charset_value_rejected_even_with_separator_preservation(
    data, key, tweak
) -> None:
    """`FpeUnencryptableError`: a value with ZERO in-charset characters has
    nothing to format-preserving-encrypt, so it fails closed even under
    `preserve_separators=True` (which otherwise tolerates PARTIAL
    out-of-charset content -- see the separators property above)."""
    cs, val = data
    with pytest.raises(FpeUnencryptableError) as ei:
        fpe_encrypt_value(val, key, cs, tweak, preserve_separators=True)
    assert ei.value.code == "fpe.unencryptable"
    assert "no character in the configured" in str(ei.value)


# --------------------------------------------------------------------------
# Boundary: empty string, single character, sub-minimum validate_luhn length
# --------------------------------------------------------------------------


@given(_charset(), _KEYS, _TWEAKS, st.booleans(), st.booleans())
def test_empty_string_is_the_only_documented_passthrough(
    cs, key, tweak, preserve_sep, validate_luhn
) -> None:
    """`_fpe_value`/`_fpe_pure_value` docstrings: an empty value carries no
    PII and is explicitly passed through unchanged -- the ONLY passthrough
    this module allows (every other unencryptable case fails closed, see
    the domain-validation properties above)."""
    assert fpe_encrypt_value("", key, cs, tweak, preserve_sep, validate_luhn) == ""
    assert fpe_decrypt_value("", key, cs, tweak, preserve_sep, validate_luhn) == ""


@given(_charset(), _KEYS, _TWEAKS, st.data())
def test_single_character_uses_the_documented_rotation_and_stays_bijective(
    cs, key, tweak, data
) -> None:
    """QA-10 F2 (2026-06-01): the degenerate 1-character case uses a
    dedicated keyed rotation (`_single_char_shift`), documented as "a
    uniform alphabet rotation; trivially bijective." This is the module's
    OWN minimum-length boundary (not FF1's minlen=2 -- see the module's
    "Design note")."""
    ch = data.draw(st.sampled_from(cs))
    enc = fpe_encrypt_value(ch, key, cs, tweak)
    assert len(enc) == 1
    assert enc in cs
    assert fpe_decrypt_value(enc, key, cs, tweak) == ch


@given(_KEYS, _TWEAKS, st.integers(min_value=0, max_value=1), st.data())
def test_validate_luhn_below_minimum_length_falls_back_to_plain_permute(
    key, tweak, length, data
) -> None:
    """`_fpe_pure_value`'s `if validate_luhn and len(s) >= 2` guard: a
    value shorter than 2 characters has no separate check-digit position to
    reserve, so `validate_luhn=True` is silently equivalent to
    `validate_luhn=False` at length 0-1 (encodes the module's OWN
    length boundary for Luhn mode, distinct from the checksum-mode minimums
    in `_fpe_checksum.py`, which fail closed instead)."""
    digits = _CHARSETS["digits"]
    val = "".join(data.draw(st.lists(st.sampled_from(digits), min_size=length, max_size=length)))
    with_luhn = fpe_encrypt_value(val, key, digits, tweak, validate_luhn=True)
    without_luhn = fpe_encrypt_value(val, key, digits, tweak, validate_luhn=False)
    assert with_luhn == without_luhn


@given(_KEYS, _TWEAKS, st.integers(min_value=2, max_value=16), st.data())
def test_validate_luhn_output_last_digit_is_the_luhn_check_digit_of_the_body(
    key, tweak, length, data
) -> None:
    """Composition property: at/above the length-2 floor, `validate_luhn`'s
    output is checksum-valid BY CONSTRUCTION -- its last digit is exactly
    `_luhn_check_digit` of everything before it. Property-generalizes the
    hand-picked PAN example in `test_fpe_roundtrip.py` over random bodies
    and lengths."""
    digits = _CHARSETS["digits"]
    val = "".join(data.draw(st.lists(st.sampled_from(digits), min_size=length, max_size=length)))
    enc = fpe_encrypt_value(val, key, digits, tweak, validate_luhn=True)
    assert enc[-1] == _luhn_check_digit(enc[:-1])


# --------------------------------------------------------------------------
# Checksum mode: random-body invertibility (luhn scheme -- no pinned
# prefix/fixed length, so it is the one scheme tractable for property
# generation; the other schemes' pinned-prefix/exact-length shapes are
# already covered by hand-picked examples in test_fpe_checksum_validity.py)
# --------------------------------------------------------------------------


@given(_KEYS, _TWEAKS, st.integers(min_value=1, max_value=15), st.data())
def test_checksum_luhn_scheme_round_trips_for_random_valid_bodies(
    key, tweak, body_len, data
) -> None:
    """`_fpe_checksum_permute`'s luhn branch: valid-by-construction output,
    and (being symmetric in both directions per its own docstring) an exact
    round trip for a source that was already Luhn-valid. Luhn: Hans Peter
    Luhn, US Patent 2,950,048 (1954)."""
    digits = _CHARSETS["digits"]
    body = "".join(
        data.draw(st.lists(st.sampled_from(digits), min_size=body_len, max_size=body_len))
    )
    value = body + checksums.calc_check_digit("luhn", body)
    enc = fpe_encrypt_value(value, key, digits, tweak, checksum="luhn")
    assert checksums.validate("luhn", enc)
    assert fpe_decrypt_value(enc, key, digits, tweak, checksum="luhn") == value
