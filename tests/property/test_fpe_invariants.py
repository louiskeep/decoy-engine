"""Property + metamorphic invariants for the FPE (format-preserving
encryption) mask strategy: the highest-blast-radius crypto module in the
engine, held to the 100% mutation bar (see the crypto/RI mandate in
`docs/quality/module-test-quality-playbook.md`).

The existing covering suite (`tests/codspeed/test_fpe_transform.py`,
`tests/unit/plan/test_check_fpe_charset.py`, `tests/unit/transforms/
test_fpe_roundtrip.py`, `test_fpe_checksum_validity.py`,
`test_fpe_remap_orphan_charset.py`, and the Hypothesis round-trip in
`tests/property/test_mask_invariants.py::test_fpe_decrypt_inverts_encrypt`)
already example-tests and property-tests format preservation and plain-mode
invertibility. `tests/unit/transforms/test_ff1_primitive.py` separately
KAT-locks and differentially tests the FF1 primitive itself
(`transforms/_ff1.py`) against NIST's own published vectors, an external
corpus, and an independently authored oracle; that is not this file's
concern. This module layers on the wrapper (`transforms/fpe.py`) instead:
determinism as a first-class assertion, key/tweak sensitivity, out-of-charset
domain validation, the FF1 minimum-domain floor, charset-duplicate rejection,
and checksum/Luhn round-trips over random bodies instead of hand-picked
examples, broadening the charset domain to synthesized custom charsets, not
just the 5 named ones.

Invariant sources (cite-the-source-pattern, per repo CLAUDE.md):

- FORMAT PRESERVATION + INVERTIBILITY: `transforms/fpe.py` module docstring
  ("Replaces each string value with another string of the same length over
  the same character set"). FF1 is a keyed permutation over its numeral-string
  domain (NIST SP 800-38G, Algorithms 5/6; see `transforms/_ff1.py`), so it is
  a bijection by construction. This is the killer metamorphic property: a
  bijection that isn't actually invertible is a broken cipher, and almost any
  mutant that corrupts the round arithmetic breaks the round trip.
- DETERMINISM: the fpe strategy's keyed-determinism contract ("same input +
  same key -> same output"), the same contract `HashStrategy` and
  `DateShiftStrategy` share.
- KEY/TWEAK SENSITIVITY: implied by the cipher being keyed at all (a PRF that
  ignores its key is not a PRF); NIST SP 800-38G treats the tweak as part of
  the encryption's identity for the same reason. Domain is gated to the FF1
  minimum admissible domain (radix^length >= 1,000,000) so a coincidental
  collision across two random keys is negligible.
- DOMAIN VALIDATION: `FpeUnencryptableError`'s docstring in
  `decoy_engine/errors.py`. Two families: the DE-01 cluster-C guards
  (all-out-of-charset always closed; any-out-of-charset closed under
  `preserve_separators=False`), unchanged by the FF1 swap, and the FF1
  profile guards added in `transforms/fpe.py::_permute` (key size, radix
  range, charset-duplicate rejection, the minimum-domain floor, length/tweak
  caps), which are new to this file.
- CHECKSUM INVALID SOURCE: `transforms/_fpe_checksum.py`'s
  `_fpe_checksum_permute` validates the complete source identifier (check
  digit included) via `checksums.validate()` before permuting, forward
  direction only; an invalid source fails closed with `FpeChecksumError`.
- BOUNDARY: empty string is an explicit documented passthrough
  (`_fpe_value`/`_fpe_pure_value` docstrings, "Empty-string preserve"). A
  single in-charset character has no FF1 equivalent to the retired Feistel's
  dedicated rotation: FF1's algorithm itself requires a numeral string of
  length >= 2, and every charset radix in this engine (2-64) puts a
  single-character domain (radix^1 <= 64) below the 1,000,000 floor anyway,
  so length 1 always fails closed on the domain floor now. Length < 2 under
  `validate_luhn=True` used to silently fall back to the plain permutation
  (the `len(s) >= 2` guard in `_fpe_pure_value`); at length 1 that fallback
  now also fails closed on the domain floor (length 0 is unaffected, since
  the empty-string passthrough runs first).
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
from decoy_engine.errors import FpeChecksumError, FpeUnencryptableError
from decoy_engine.transforms._ff1 import FF1_KEY_BYTES, min_domain_length
from decoy_engine.transforms.fpe import (
    _CHARSETS,
    _char_lookup,
    _luhn_check_digit,
    check_charset_unique,
    fpe_decrypt_value,
    fpe_encrypt_value,
    resolve_fpe_charset,
)

# Match the pilot's audit profile: more examples than the 100-example
# default, no deadline (an admissible-domain value can run up to ~40
# characters through 10 rounds of AES-backed FF1, and Hypothesis shrinking
# can trip the 200ms wall), and print_blob so a counterexample is
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

_KEYS = st.binary(min_size=FF1_KEY_BYTES, max_size=FF1_KEY_BYTES)
_TWEAKS = st.binary(min_size=0, max_size=32)


@st.composite
def _charset(draw: st.DrawFn) -> str:
    """One of the engine's 5 named charsets, or a synthesized custom one
    (2-20 distinct characters), the two shapes `resolve_fpe_charset` resolves
    a `charset:` config value to (`_CHARSETS.get(spec, spec)`)."""
    if draw(st.booleans()):
        return draw(st.sampled_from(_NAMED_CHARSETS))
    size = draw(st.integers(min_value=2, max_value=20))
    chars = draw(st.lists(st.sampled_from(_CUSTOM_POOL), unique=True, min_size=size, max_size=size))
    return "".join(chars)


@st.composite
def _charset_and_value(draw: st.DrawFn, min_len: int = 0, max_len: int = 24) -> tuple[str, str]:
    """A charset plus a value made ENTIRELY of that charset's characters, of
    an arbitrary length (may be below the FF1 domain floor; use
    `_charset_and_admissible_value` when the test needs encryption to
    succeed)."""
    cs = draw(_charset())
    n = draw(st.integers(min_value=min_len, max_value=max_len))
    value = "".join(draw(st.lists(st.sampled_from(cs), min_size=n, max_size=n)))
    return cs, value


@st.composite
def _charset_and_admissible_value(draw: st.DrawFn, extra_max: int = 20) -> tuple[str, str]:
    """A charset plus a value whose length clears the FF1 minimum admissible
    domain for that charset's radix (NIST SP 800-38G's own minimum-domain
    floor, `FF1_MIN_DOMAIN`), so `fpe_encrypt_value` succeeds and a
    coincidental collision across two independently generated keys/tweaks is
    negligible (< 1e-6). Drawn at the floor length plus 0-`extra_max` extra
    characters, for coverage above the boundary too."""
    cs = draw(_charset())
    floor = min_domain_length(len(cs))
    n = draw(st.integers(min_value=floor, max_value=floor + extra_max))
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
    """A charset plus a value interleaving an ADMISSIBLE-length run of
    in-charset characters with out-of-charset "separator" characters at
    random positions, exercising `preserve_separators=True`'s partial-content
    contract. Only the in-charset characters count toward the FF1 domain
    (separators are reinserted verbatim, never permuted), so the body drawn
    here clears the floor the same way `_charset_and_admissible_value` does."""
    cs = draw(_charset())
    sep_candidates = _poison_candidates(cs)
    floor = min_domain_length(len(cs))
    n_body = draw(st.integers(min_value=floor, max_value=floor + 12))
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


@given(_charset_and_admissible_value(), _KEYS, _TWEAKS)
def test_format_preservation_length_and_alphabet(data, key, tweak) -> None:
    """Module docstring: 'Replaces each string value with another string of
    the same length over the same character set.'"""
    cs, val = data
    enc = fpe_encrypt_value(val, key, cs, tweak)
    assert len(enc) == len(val)
    assert all(ch in cs for ch in enc)


@given(_charset_and_admissible_value(), _KEYS, _TWEAKS)
def test_invertibility_decrypt_undoes_encrypt(data, key, tweak) -> None:
    """The killer property: `fpe_decrypt_value(fpe_encrypt_value(x)) == x`
    for every admissible-domain input, over a broader charset domain
    (including synthesized custom charsets) than the existing example-based
    suite. FF1 is a bijection by construction (NIST SP 800-38G); almost any
    mutant to the round arithmetic breaks this."""
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


@given(_charset_and_admissible_value(), _KEYS, _TWEAKS, st.booleans())
def test_determinism_same_key_and_tweak_same_output(data, key, tweak, preserve_sep) -> None:
    """Same input + same key -> same output (keyed determinism): encrypting
    the same value twice under the same (key, charset, tweak, config) must be
    byte-identical, since this is what makes cross-run/cross-process
    fingerprints stable."""
    cs, val = data
    a = fpe_encrypt_value(val, key, cs, tweak, preserve_separators=preserve_sep)
    b = fpe_encrypt_value(val, key, cs, tweak, preserve_separators=preserve_sep)
    assert a == b


# --------------------------------------------------------------------------
# Key / tweak sensitivity (no key-independence leak)
# --------------------------------------------------------------------------


_SENSITIVITY_SAMPLE_SIZE = 8


@given(_charset_and_admissible_value(), _TWEAKS, st.data())
def test_different_key_changes_ciphertext(data, tweak, hyp_data) -> None:
    """A cipher whose output does not depend on the key is not keyed at
    all, which would mean anyone (not just the key holder) could predict
    the mapping. A single pair of independent random keys colliding on one
    value is legal for a permutation family (not every key pair need land
    on a different output), so the universal per-sample inequality is not
    a valid invariant; the real invariant is aggregate. Draw several
    independent key pairs for the same value/tweak: if the cipher were
    key-independent, EVERY pair would collide, which the domain floor
    (>= 1e6) makes a vanishingly unlikely accident otherwise."""
    cs, val = data
    pairs = []
    for _ in range(_SENSITIVITY_SAMPLE_SIZE):
        key_a = hyp_data.draw(_KEYS)
        key_b = hyp_data.draw(_KEYS)
        assume(key_a != key_b)
        pairs.append((key_a, key_b))
    outputs = [
        (fpe_encrypt_value(val, key_a, cs, tweak), fpe_encrypt_value(val, key_b, cs, tweak))
        for key_a, key_b in pairs
    ]
    assert any(enc_a != enc_b for enc_a, enc_b in outputs), (
        f"{_SENSITIVITY_SAMPLE_SIZE} independent key pairs all produced identical "
        "ciphertext for the same value and tweak: output does not depend on the key"
    )


@given(_charset_and_admissible_value(), _KEYS, st.data())
def test_different_tweak_changes_ciphertext(data, key, hyp_data) -> None:
    """NIST SP 800-38G treats the tweak as part of the encryption's identity
    (per-column tweaking, this engine's `fpe_join_group`/column-name tweak,
    would leak cross-column correlations if the tweak had no effect). A
    single pair of tweaks colliding on one value is legal for a permutation
    family, so, as with key sensitivity above, the invariant is aggregate
    over several independent tweak pairs rather than a universal per-pair
    claim."""
    cs, val = data
    pairs = []
    for _ in range(_SENSITIVITY_SAMPLE_SIZE):
        tweak_a = hyp_data.draw(_TWEAKS)
        tweak_b = hyp_data.draw(_TWEAKS)
        assume(tweak_a != tweak_b)
        pairs.append((tweak_a, tweak_b))
    outputs = [
        (fpe_encrypt_value(val, key, cs, tweak_a), fpe_encrypt_value(val, key, cs, tweak_b))
        for tweak_a, tweak_b in pairs
    ]
    assert any(enc_a != enc_b for enc_a, enc_b in outputs), (
        f"{_SENSITIVITY_SAMPLE_SIZE} independent tweak pairs all produced identical "
        "ciphertext for the same value and key: output does not depend on the tweak"
    )


# --------------------------------------------------------------------------
# No-op leakage
# --------------------------------------------------------------------------


@given(_charset_and_admissible_value(), st.data())
def test_no_op_leakage_ciphertext_differs_from_plaintext(data, hyp_data) -> None:
    """A mask strategy that returns its input unchanged protects nothing.
    A fixed point (ciphertext == plaintext) is legal for a permutation, so
    a universal per-sample claim is not a valid invariant; the real bug
    this guards against is a strategy that is ALWAYS a no-op. Draw several
    independent (key, tweak) pairs for the same value and require they do
    not ALL land on the identity (below the FF1 floor a real fixed-point
    chance exists, see `test_single_in_charset_character_*`, which is
    exactly why that domain is rejected outright rather than left to
    chance)."""
    cs, val = data
    samples = [
        (hyp_data.draw(_KEYS), hyp_data.draw(_TWEAKS)) for _ in range(_SENSITIVITY_SAMPLE_SIZE)
    ]
    outputs = [fpe_encrypt_value(val, key, cs, tweak) for key, tweak in samples]
    assert any(enc != val for enc in outputs), (
        f"{_SENSITIVITY_SAMPLE_SIZE} independent (key, tweak) pairs all reproduced "
        "the plaintext unchanged: the strategy is a no-op"
    )


# --------------------------------------------------------------------------
# Domain validation: the DE-01 cluster-C guards (unchanged by the FF1 swap)
# --------------------------------------------------------------------------


@given(_charset_with_poison(), st.integers(min_value=0, max_value=10), _KEYS, _TWEAKS, st.data())
def test_out_of_charset_character_rejected_without_separator_preservation(
    cs_poison, body_len, key, tweak, data
) -> None:
    """`FpeUnencryptableError` (DE-01 cluster-C): under
    `preserve_separators=False`, ANY out-of-charset character fails closed
    (the pre-fix path silently returned the value unchanged, a cleartext
    leak). One poisoned position in an otherwise-valid random body is
    enough to trip it. This guard fires before any FF1 call, so it is
    independent of the domain floor and unaffected by the cipher swap."""
    cs, poison = cs_poison
    body = "".join(data.draw(st.lists(st.sampled_from(cs), min_size=body_len, max_size=body_len)))
    pos = data.draw(st.integers(min_value=0, max_value=len(body)))
    val = body[:pos] + poison + body[pos:]
    with pytest.raises(FpeUnencryptableError) as ei:
        fpe_encrypt_value(val, key, cs, tweak, preserve_separators=False)
    assert ei.value.code == "fpe.unencryptable"
    # P7: the exception carries a length, never the value itself. (No
    # `val not in str(...)` check here: `val` can be a single arbitrary
    # punctuation character, which would spuriously "match" ordinary prose
    # in the message; the dedicated redaction tests use longer, more
    # distinctive values instead.)
    assert ei.value.value_length == len(val)


@given(_all_out_of_charset_value(), _KEYS, _TWEAKS)
def test_all_out_of_charset_value_rejected_even_with_separator_preservation(
    data, key, tweak
) -> None:
    """`FpeUnencryptableError`: a value with ZERO in-charset characters has
    nothing to format-preserving-encrypt, so it fails closed even under
    `preserve_separators=True` (which otherwise tolerates PARTIAL
    out-of-charset content, see the separators property above). This guard
    also fires before any FF1 call."""
    cs, val = data
    with pytest.raises(FpeUnencryptableError) as ei:
        fpe_encrypt_value(val, key, cs, tweak, preserve_separators=True)
    assert ei.value.code == "fpe.unencryptable"
    assert "no character in the configured" in str(ei.value)


# --------------------------------------------------------------------------
# Domain validation: the FF1 profile guards (new to the FF1 swap)
# --------------------------------------------------------------------------


@given(_charset(), _KEYS, _TWEAKS, st.data())
def test_below_domain_floor_value_fails_closed(cs, key, tweak, data) -> None:
    """A non-empty, fully in-charset value shorter than
    `min_domain_length(radix)` has a domain (`radix ** length`) below FF1's
    minimum admissible domain: below that floor FF1 (and any format-
    preserving cipher) is undefined/insecure, so `_permute` fails closed
    rather than encrypt under a domain too small to be safe."""
    floor = min_domain_length(len(cs))
    assume(floor > 1)  # only charsets with a floor above the trivial 1-char case are useful here
    n = data.draw(st.integers(min_value=1, max_value=floor - 1))
    val = "".join(data.draw(st.lists(st.sampled_from(cs), min_size=n, max_size=n)))
    with pytest.raises(FpeUnencryptableError) as ei:
        fpe_encrypt_value(val, key, cs, tweak)
    assert ei.value.code == "fpe.unencryptable_domain"


@given(_charset(), _KEYS, _TWEAKS, st.data())
def test_single_in_charset_character_always_fails_the_domain_floor(cs, key, tweak, data) -> None:
    """Every charset radix in this engine is <= 64 (the deployed profile's
    ceiling), so a single in-charset character's domain (`radix ** 1 <= 64`)
    is always below the 1,000,000 floor: length 1 is unconditionally
    sub-floor, for every charset this module can construct, not merely a
    boundary case that happens to land below it."""
    ch = data.draw(st.sampled_from(cs))
    with pytest.raises(FpeUnencryptableError) as ei:
        fpe_encrypt_value(ch, key, cs, tweak)
    assert ei.value.code == "fpe.unencryptable_domain"


@given(_charset_and_value(min_len=1, max_len=40), st.data())
def test_key_length_other_than_32_bytes_fails_closed(data_pair, data) -> None:
    """FF1 is deployed AES-256-only: a key of any other length fails closed
    before any cipher arithmetic runs, whether it is short (a weaker cipher
    the engine never deploys) or long (a caller bug), which distinguishes
    "genuinely too small a domain" from "this is a config/wiring bug" via a
    dedicated code."""
    cs, val = data_pair
    assume(val)  # empty string is a passthrough regardless of key length
    bad_length = data.draw(st.integers(min_value=0, max_value=64).filter(lambda n: n != 32))
    key = bytes(bad_length)
    tweak = b"tw"
    with pytest.raises(FpeUnencryptableError) as ei:
        fpe_encrypt_value(val, key, cs, tweak)
    assert ei.value.code == "fpe.unencryptable_length"


# --------------------------------------------------------------------------
# Charset-duplicate rejection (new to the FF1 swap: the pre-FF1 engine
# silently deduplicated a custom charset; FF1 requires an ordered,
# duplicate-free alphabet and rejects a duplicate outright instead)
# --------------------------------------------------------------------------


@st.composite
def _charset_with_duplicate(draw: st.DrawFn) -> str:
    """A synthesized custom charset (2-20 unique characters) with one
    randomly chosen character duplicated, so `len(set(charset)) !=
    len(charset)`."""
    size = draw(st.integers(min_value=2, max_value=20))
    chars = draw(st.lists(st.sampled_from(_CUSTOM_POOL), unique=True, min_size=size, max_size=size))
    dup = draw(st.sampled_from(chars))
    pos = draw(st.integers(min_value=0, max_value=len(chars)))
    chars = [*chars[:pos], dup, *chars[pos:]]
    return "".join(chars)


@given(_charset_with_duplicate())
def test_check_charset_unique_rejects_a_duplicate_symbol(charset_with_dup) -> None:
    with pytest.raises(FpeUnencryptableError) as ei:
        check_charset_unique("custom", charset_with_dup)
    assert ei.value.code == "fpe.unencryptable_length"


@given(_charset())
def test_check_charset_unique_accepts_every_charset_this_module_synthesizes(cs) -> None:
    """`_charset()` only ever produces unique-by-construction alphabets
    (named charsets are unique; the custom generator draws with
    `unique=True`), so `check_charset_unique` must accept all of them."""
    check_charset_unique("custom", cs)  # must not raise


@given(_charset_with_duplicate(), _KEYS, _TWEAKS, st.data())
def test_permute_itself_rejects_a_duplicate_charset_even_unfiltered(
    charset_with_dup, key, tweak, data
) -> None:
    """`_permute`'s own duplicate check is a backstop independent of
    `check_charset_unique`: a caller that reaches `fpe_encrypt_value`
    directly with a duplicate-symbol charset (bypassing the resolve/check
    helpers a strategy handler would normally call) still fails closed,
    rather than silently permuting over an ambiguous alphabet."""
    radix = len(set(charset_with_dup))
    floor = min_domain_length(radix)
    n = data.draw(st.integers(min_value=floor, max_value=floor + 5))
    val = "".join(data.draw(st.lists(st.sampled_from(charset_with_dup), min_size=n, max_size=n)))
    with pytest.raises(FpeUnencryptableError) as ei:
        fpe_encrypt_value(val, key, charset_with_dup, tweak)
    assert ei.value.code == "fpe.unencryptable_length"


def test_resolve_fpe_charset_named_vs_literal() -> None:
    """A named spec resolves through `_CHARSETS`; anything else is taken as
    a literal charset string, unchanged."""
    assert resolve_fpe_charset("digits") == _CHARSETS["digits"]
    assert resolve_fpe_charset("ALPHANUM") == _CHARSETS["ALPHANUM"]
    assert resolve_fpe_charset("xzq!") == "xzq!"


# --------------------------------------------------------------------------
# Boundary: empty string, and the retired single-character rotation
# --------------------------------------------------------------------------


@given(_charset(), _KEYS, _TWEAKS, st.booleans(), st.booleans())
def test_empty_non_null_value_fails_closed(cs, key, tweak, preserve_sep, validate_luhn) -> None:
    """Plan P2: a present-but-empty value is not a null (nulls never reach
    this layer; they are skipped one layer up via `na_mask`) and its
    in-charset domain is `radix**0 == 1`, below the FF1 minimum admissible
    domain like any other sub-floor value. The engine used to pass this
    through unchanged as a documented carve-out; that carve-out is gone --
    empty now fails closed the same as every other unencryptable case."""
    with pytest.raises(FpeUnencryptableError) as enc_ei:
        fpe_encrypt_value("", key, cs, tweak, preserve_sep, validate_luhn)
    assert enc_ei.value.code == "fpe.unencryptable_domain"
    with pytest.raises(FpeUnencryptableError) as dec_ei:
        fpe_decrypt_value("", key, cs, tweak, preserve_sep, validate_luhn)
    assert dec_ei.value.code == "fpe.unencryptable_domain"


@given(_KEYS, _TWEAKS, st.data())
def test_validate_luhn_at_length_zero_fails_closed_regardless(key, tweak, data) -> None:
    """At length 0, `validate_luhn` cannot change anything: the empty-value
    rejection in `_fpe_value` runs before `_fpe_pure_value`'s `validate_luhn`
    check is ever reached, for both settings."""
    digits = _CHARSETS["digits"]
    for validate_luhn in (True, False):
        with pytest.raises(FpeUnencryptableError) as ei:
            fpe_encrypt_value("", key, digits, tweak, validate_luhn=validate_luhn)
        assert ei.value.code == "fpe.unencryptable_domain"


@given(_KEYS, _TWEAKS, st.data())
def test_validate_luhn_at_length_one_fails_the_domain_floor_either_way(key, tweak, data) -> None:
    """`_fpe_pure_value`'s `if validate_luhn and len(s) >= 2` guard means a
    length-1 value falls through to the plain `_permute` call regardless of
    `validate_luhn` (there is no separate check-digit position to reserve
    below length 2). Under FF1, that plain call now fails the domain floor
    the same way for both settings: `validate_luhn` does not change the
    failure mode at this length."""
    digits = _CHARSETS["digits"]
    val = data.draw(st.sampled_from(digits))
    for validate_luhn in (True, False):
        with pytest.raises(FpeUnencryptableError) as ei:
            fpe_encrypt_value(val, key, digits, tweak, validate_luhn=validate_luhn)
        assert ei.value.code == "fpe.unencryptable_domain"


@given(_KEYS, _TWEAKS, st.integers(min_value=7, max_value=17), st.data())
def test_validate_luhn_output_last_digit_is_the_luhn_check_digit_of_the_body(
    key, tweak, length, data
) -> None:
    """Composition property: at/above the length floor (radix 10 needs
    `min_domain_length(10) == 6` for the body, so `length >= 7` keeps the
    6+-digit body admissible), `validate_luhn`'s output is checksum-valid BY
    CONSTRUCTION: its last digit is exactly `_luhn_check_digit` of everything
    before it. Property-generalizes the hand-picked PAN example in
    `test_fpe_roundtrip.py` over random bodies and lengths."""
    digits = _CHARSETS["digits"]
    assert min_domain_length(10) == 6
    val = "".join(data.draw(st.lists(st.sampled_from(digits), min_size=length, max_size=length)))
    enc = fpe_encrypt_value(val, key, digits, tweak, validate_luhn=True)
    assert enc[-1] == _luhn_check_digit(enc[:-1])


# --------------------------------------------------------------------------
# Checksum mode: random-body invertibility (luhn scheme, no pinned
# prefix/fixed length, so it is the one scheme tractable for property
# generation; the other schemes' pinned-prefix/exact-length shapes are
# already covered by hand-picked examples in test_fpe_checksum_validity.py)
# --------------------------------------------------------------------------


@given(_KEYS, _TWEAKS, st.integers(min_value=6, max_value=20), st.data())
def test_checksum_luhn_scheme_round_trips_for_random_valid_bodies(
    key, tweak, body_len, data
) -> None:
    """`_fpe_checksum_permute`'s luhn branch: valid-by-construction output,
    and (being symmetric in both directions per its own docstring) an exact
    round trip for a source that was already Luhn-valid. `body_len` starts
    at `min_domain_length(10) == 6` so the permuted body clears the FF1
    domain floor. Luhn: Hans Peter Luhn, US Patent 2,950,048 (1954)."""
    digits = _CHARSETS["digits"]
    body = "".join(
        data.draw(st.lists(st.sampled_from(digits), min_size=body_len, max_size=body_len))
    )
    value = body + checksums.calc_check_digit("luhn", body)
    enc = fpe_encrypt_value(value, key, digits, tweak, checksum="luhn")
    assert checksums.validate("luhn", enc)
    assert fpe_decrypt_value(enc, key, digits, tweak, checksum="luhn") == value


@given(_KEYS, _TWEAKS, st.integers(min_value=6, max_value=20), st.data())
def test_checksum_luhn_scheme_rejects_an_invalid_source_check_digit(
    key, tweak, body_len, data
) -> None:
    """Task 5.2 plan P5-final: the SOURCE value's check digit must already
    be valid before any permutation runs (forward direction only). A body
    with a check digit that does NOT match it fails closed with
    `FpeChecksumError` (code `fpe.checksum_invalid_source`) rather than
    silently permuting an already-invalid identifier."""
    digits = _CHARSETS["digits"]
    body = "".join(
        data.draw(st.lists(st.sampled_from(digits), min_size=body_len, max_size=body_len))
    )
    correct_check_digit = checksums.calc_check_digit("luhn", body)
    wrong_check_digit = data.draw(
        st.sampled_from(digits).filter(lambda d: d != correct_check_digit)
    )
    value = body + wrong_check_digit
    with pytest.raises(FpeChecksumError) as ei:
        fpe_encrypt_value(value, key, digits, tweak, checksum="luhn")
    assert ei.value.code == "fpe.checksum_invalid_source"


# --------------------------------------------------------------------------
# Named-checksum wiring (example-based: fixed prefixes/lengths per scheme,
# not property-tractable the way luhn's free-form body is)
# --------------------------------------------------------------------------


def test_out_of_charset_rejection_message_lists_the_offending_character_count() -> None:
    """The `preserve_separators=False` rejection message's load-bearing
    diagnostic content is the COUNT of distinct out-of-charset characters
    (per the playbook's "assert the load-bearing parts of a message"
    guidance), not the characters themselves: P7 forbids echoing source
    value content in an exception message, so the count is what callers
    get to size up their charset config."""
    with pytest.raises(FpeUnencryptableError) as ei:
        fpe_encrypt_value("1a2b3", b"k" * 32, _CHARSETS["digits"], b"tw", preserve_separators=False)
    assert "2 distinct out-of-charset character(s)" in str(ei.value)


def test_length_invariant_guard_fails_closed_with_the_offending_value(monkeypatch) -> None:
    """The internal 'permuted body length != positions' guard is a
    belt-and-suspenders defense that should be unreachable via any current
    public-API input (the upstream checksum/Luhn length validation already
    prevents a mismatch), exercised directly by forcing `_fpe_pure_value` to
    return a wrong-length body, confirming it still fails closed with the
    correct error `code` and a length derived from the offending value (P7:
    never the value itself, since that is what would leak through a caller
    inspecting or logging the exception)."""
    import decoy_engine.transforms.fpe as fpe_mod

    monkeypatch.setattr(fpe_mod, "_fpe_pure_value", lambda *a, **k: "short")
    val = "12-34"
    with pytest.raises(FpeUnencryptableError) as ei:
        fpe_encrypt_value(val, b"k" * 32, _CHARSETS["digits"], b"tw", preserve_separators=True)
    assert ei.value.code == "fpe.unencryptable"
    assert ei.value.value_length == len(val)
    assert val not in str(ei.value)
    assert val not in repr(ei.value)


def test_preserve_separators_false_round_trips_the_real_value() -> None:
    """The final `_fpe_pure_value` call in the `preserve_separators=False`
    branch must forward the ACTUAL value and the ACTUAL `forward` flag, not
    `None`, not a placeholder. A `forward=None` mutant here silently makes
    ENCRYPT use decrypt-direction math; decrypt then composes two inverse
    applications instead of one forward + one inverse, which breaks
    invertibility even though each call individually "succeeds" without
    error. A `val=None` mutant hits `_fpe_pure_value`'s empty-string
    passthrough (`not None` is truthy) and returns `None` outright."""
    val = "1234567890"
    key = b"k" * 32
    digits = _CHARSETS["digits"]
    enc = fpe_encrypt_value(val, key, digits, b"tw", preserve_separators=False)
    assert len(enc) == len(val)
    assert fpe_decrypt_value(enc, key, digits, b"tw", preserve_separators=False) == val


def test_preserve_separators_false_forwards_validate_luhn_true() -> None:
    """Same final call as above: `validate_luhn` must reach
    `_fpe_pure_value` un-substituted, since `None` is falsy and would
    silently behave like `validate_luhn=False`, skipping the Luhn
    check-digit append."""
    val = "1234567890"
    key = b"k" * 32
    digits = _CHARSETS["digits"]
    enc = fpe_encrypt_value(val, key, digits, b"tw", preserve_separators=False, validate_luhn=True)
    assert enc[-1] == _luhn_check_digit(enc[:-1])


def test_preserve_separators_false_forwards_checksum_scheme() -> None:
    """Same final call as above: `checksum` must reach `_fpe_pure_value`
    un-substituted and un-dropped, since either failure would silently fall
    back to plain permutation, producing output that is NOT checksum-valid."""
    digits = _CHARSETS["digits"]
    body = "123456789"
    value = body + checksums.calc_check_digit("luhn", body)
    key = b"k" * 32
    enc = fpe_encrypt_value(value, key, digits, b"tw", preserve_separators=False, checksum="luhn")
    assert checksums.validate("luhn", enc)
    assert (
        fpe_decrypt_value(enc, key, digits, b"tw", preserve_separators=False, checksum="luhn")
        == value
    )


def test_encrypt_defaults_to_preserving_separators() -> None:
    """`fpe_encrypt_value`'s documented default (`preserve_separators: bool
    (default: true)`): callers that omit the argument must get separator-
    preserving behavior, not a silent switch to `preserve_separators=False`
    (which would fail closed on this value's dashes instead)."""
    val = "123-45-6789"
    key = b"k" * 32
    digits = _CHARSETS["digits"]
    enc = fpe_encrypt_value(val, key, digits, b"tw")  # relies on the default
    assert enc[3] == "-" and enc[6] == "-"
    assert fpe_decrypt_value(enc, key, digits, b"tw", preserve_separators=True) == val


def test_decrypt_defaults_to_preserving_separators() -> None:
    """Same default, `fpe_decrypt_value` side."""
    val = "123-45-6789"
    key = b"k" * 32
    digits = _CHARSETS["digits"]
    enc = fpe_encrypt_value(val, key, digits, b"tw", preserve_separators=True)
    assert fpe_decrypt_value(enc, key, digits, b"tw") == val  # relies on the default


def test_luhn_check_digit_matches_known_answer_vectors() -> None:
    """Independent reference, not derived by calling `_luhn_check_digit`
    itself: the worked examples from the Luhn algorithm's public description
    (Hans Peter Luhn, US Patent 2,950,048, 1954; see e.g. Wikipedia's "Luhn
    algorithm" article). Payload 7992739871 check-digits to 3, giving the
    well-known valid number 79927398713; the other two vectors together
    exercise every step of the algorithm: the running total's start value,
    which positions get doubled (odd vs even, in both directions), the
    doubling itself, the >9 correction, and the final
    `(10 - total % 10) % 10` formula."""
    for body, expected in (("7992739871", "3"), ("25", "7"), ("123456789", "7")):
        assert _luhn_check_digit(body) == expected


def test_char_lookup_builds_a_complete_index_for_a_custom_charset() -> None:
    """`_char_lookup`'s cache-miss branch (a charset not in the module's 5
    named `_CHARSETS`, so `_CHARSET_INDEX` has no precomputed entry) must
    still build and return the full {char: index} mapping, not skip the
    build and return the cache-miss sentinel unchanged."""
    charset = "qzxjk"
    assert _char_lookup(charset) == {ch: i for i, ch in enumerate(charset)}
