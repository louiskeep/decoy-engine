"""Format-Preserving Encryption (FPE) mask strategy.

Replaces each string value with another string of the same length over the
same character set. Same input + same key produces the same output across
runs and instances (keyed determinism via the existing derive_key path,
identical to HashStrategy and DateShiftStrategy).

Algorithm (Task 5.2, Option C): NIST SP 800-38G FF1 (Algorithms 5/6), the
sole surviving NIST format-preserving-encryption method after the Rev.1
second public draft withdrew FF3/FF3-1 (Beyne 2021 attack). The primitive
lives in ``transforms/_ff1.py`` (normative to the standard, KAT-locked
against NIST's own published samples plus an external corpus; see that
module's tests). This module is the deployable-profile wrapper: it resolves
a value's charset into the numeral alphabet FF1 needs, enforces the pinned
validation order (radix bounds, domain floor, length/tweak caps, key size)
before ever calling the primitive, and reinserts the encrypted numerals back
into the original in-charset positions. FF1 replaces the engine's earlier
home-rolled 8-round HMAC-SHA256 Feistel construction entirely (pre-GA hard
cutover, ``SEED_PROTOCOL_VERSION`` 6 -> 7); the Feistel code no longer exists
anywhere in this module.

Pattern: NIST SP 800-38G FF1, AES-256 via the audited `cryptography`
package (see ``transforms/_ff1.py`` for the full citation and KAT
provenance).
"""

from __future__ import annotations

from typing import Final

from decoy_engine.errors import FpeUnencryptableError
from decoy_engine.transforms import _ff1

_CHARSETS: dict[str, str] = {
    "digits": "0123456789",
    "alpha": "abcdefghijklmnopqrstuvwxyz",
    "ALPHA": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "alphanum": "0123456789abcdefghijklmnopqrstuvwxyz",
    "ALPHANUM": "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
}

# F5 fix: pre-computed {char: index} lookup per named charset so `_permute`'s
# body-to-numerals conversion is O(n) per string instead of O(n * r) (where r
# = |charset|). Keyed on the charset STRING (not the name) so a custom
# charset can still look up without a reverse mapping.
_CHARSET_INDEX: dict[str, dict[str, int]] = {
    chars: {ch: i for i, ch in enumerate(chars)} for chars in _CHARSETS.values()
}


def _char_lookup(charset: str) -> dict[str, int]:
    lookup = _CHARSET_INDEX.get(charset)
    if lookup is None:
        lookup = {ch: i for i, ch in enumerate(charset)}
    return lookup


def resolve_fpe_charset(charset_spec: str) -> str:
    """Resolve a charset spec (a named preset or a literal string) the same
    way every fpe call site does: named charsets pass through ``_CHARSETS``
    unchanged (already unique by construction); a literal charset is taken
    AS GIVEN.

    Every named entry in ``_CHARSETS`` is unique by construction. A custom
    charset is NOT deduplicated here (Task 5.2 plan v2 body: "ordered UNIQUE
    alphabet only; reject duplicate-symbol alphabets, do not dedupe as
    today"); call ``check_charset_unique`` on the result before use so a
    duplicate is a loud config error instead of a silent collapse.
    """
    return _CHARSETS.get(charset_spec, charset_spec)


def check_charset_unique(charset_spec: str, charset: str) -> None:
    """Raise ``FpeUnencryptableError`` if ``charset`` has a duplicate symbol.

    FF1 requires an ordered, duplicate-free alphabet: a repeated character
    would make two distinct numerals decode to the same output symbol,
    silently losing information. The pre-FF1 engine dedup'd a custom
    charset instead of rejecting it; Task 5.2 makes that a fail-closed
    config error (code ``fpe.unencryptable_length``) at every call site
    that resolves an fpe charset, both at plan-compile time and as the
    execution-time backstop.
    """
    if len(set(charset)) != len(charset):
        raise FpeUnencryptableError(
            f"fpe charset {charset_spec!r} -> {charset!r} contains duplicate "
            "symbols; FF1 requires an ordered, duplicate-free alphabet. Remove "
            "the repeated character(s) from the charset.",
            code="fpe.unencryptable_length",
        )


_PRINTABLE_ASCII_MIN = 0x21  # "!"
_PRINTABLE_ASCII_MAX = 0x7E  # "~"


def check_charset_ascii_printable(charset: str) -> None:
    """Raise ``FpeUnencryptableError`` if ``charset`` has a code point outside
    printable ASCII (0x21-0x7E).

    Task 5.2 plan P6-final: every named entry in ``_CHARSETS`` is already
    printable ASCII by construction, so this only ever rejects a CUSTOM
    charset. Restricting to this range sidesteps Unicode grapheme
    segmentation entirely (one code point is one symbol, trivially, within
    this range); a custom alphabet drawing on non-ASCII, control, or
    whitespace characters is rejected outright rather than accepted and
    mishandled. The message never echoes the rejected character(s)
    themselves (only their count and code points as hex, which are public
    alphabet metadata, not customer data).
    """
    offenders = sorted(
        {ch for ch in charset if not (_PRINTABLE_ASCII_MIN <= ord(ch) <= _PRINTABLE_ASCII_MAX)}
    )
    if offenders:
        codepoints = ", ".join(f"U+{ord(ch):04X}" for ch in offenders)
        raise FpeUnencryptableError(
            f"fpe charset has {len(offenders)} character(s) outside printable "
            f"ASCII (0x21-0x7E): {codepoints}. Custom fpe charsets are "
            "restricted to printable ASCII so one code point is always "
            "exactly one symbol; use only characters in that range.",
            code="fpe.unencryptable_length",
        )


# ---------------------------------------------------------------------------
# FF1 tweak wire format (Task 5.2 plan P1): the framing IS the FF1 tweak.
#
#   tweak = VERSION (1 byte, 0x01) || SCOPE (1 byte) || LEN (uint16 BE)
#            || identity_utf8
#
# `identity_utf8` is the column/join-group/span identity, encoded STRICT
# UTF-8 (no `errors="replace"`, no Unicode normalization: two
# normalization-different identities are intentionally distinct tweaks).
# `LEN` is the byte length of `identity_utf8`; a caller distinguishes column
# vs join-group vs text-span tweaks so cross-scope collisions are impossible
# even with an identical identity string.
# ---------------------------------------------------------------------------

FF1_TWEAK_VERSION: Final[int] = 1
FF1_TWEAK_SCOPE_COLUMN: Final[int] = 0x01
FF1_TWEAK_SCOPE_JOIN_GROUP: Final[int] = 0x02
FF1_TWEAK_SCOPE_TEXT_SPAN: Final[int] = 0x03

# The constant derive() source for the per-(mask_key, namespace) FF1 AES-256
# key. Domain-separated from the retired Feistel-era `b"fpe-key/v1"` label so
# no v6 key material is ever reused under FF1 (the version bump captures the
# change). Canonical home for this constant; `execution._strategies._fpe`
# re-exports it for its existing importers, and every other FF1 call site
# (column strategy, out-of-core, unmask, text-span masking) imports it from
# here so `transforms/` never depends on `execution/` for its own primitive.
FF1_KEY_LABEL: Final[bytes] = b"ff1-key/v1"


def build_ff1_tweak(scope: int, identity: str) -> bytes:
    """Build the pinned FF1 tweak for one (scope, identity) pair.

    Raises ``FpeUnencryptableError`` (code ``fpe.unencryptable_length``) if
    the identity is too long to fit the uint16 length field, or if the
    assembled tweak exceeds the deployed profile's ``FF1_MAX_TWEAK_LEN``.
    """
    identity_bytes = identity.encode("utf-8")
    if len(identity_bytes) > 0xFFFF:
        raise FpeUnencryptableError(
            f"fpe tweak identity is {len(identity_bytes)} bytes, exceeding the "
            "uint16 length field (65535 bytes max). Use a shorter column/"
            "join_group/detector identity.",
            code="fpe.unencryptable_length",
        )
    tweak = (
        bytes([FF1_TWEAK_VERSION, scope]) + len(identity_bytes).to_bytes(2, "big") + identity_bytes
    )
    if len(tweak) > _ff1.FF1_MAX_TWEAK_LEN:
        raise FpeUnencryptableError(
            f"fpe tweak is {len(tweak)} bytes, exceeding the deployed profile's "
            f"maximum tweak length ({_ff1.FF1_MAX_TWEAK_LEN} bytes).",
            code="fpe.unencryptable_length",
        )
    return tweak


def _luhn_check_digit(body: str) -> str:
    """Compute the Luhn check digit for a digit string (without the check digit)."""
    total = 0
    for i, ch in enumerate(reversed(body)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def _permute(s: str, key: bytes, charset: str, tweak: bytes, *, forward: bool) -> str:
    """FF1-encrypt (or invert) a string made entirely of ``charset`` characters.

    Enforces the deployable FF1 profile (Task 5.2 plan P2/P2-add) in the
    pinned order, fail-closed at the first violation, before ever calling the
    primitive in ``transforms/_ff1``:

    1. key size (exactly ``FF1_KEY_BYTES`` == 32; AES-256 only).
    2. radix in ``[FF1_MIN_RADIX, FF1_MAX_RADIX]``.
    3. alphabet uniqueness (an ordered, duplicate-free charset: FF1 with a
       duplicate symbol would map two "different" numerals onto the same
       character, silently losing information on decode; NIST's own domain
       math assumes a unique alphabet).
    4. body length in ``[min_domain_length(radix), FF1_MAX_LEN]``, which
       encompasses both the FF1 minimum-admissible-domain floor (below it,
       ANY format-preserving cipher including FF1 is undefined/insecure) and
       the deployed profile's practical length ceiling.
    5. tweak length (checked again here, defense in depth; `build_ff1_tweak`
       is the primary enforcement point).
    6. body-symbol/radix agreement: every character is a member of the given
       alphabet (this is what actually produces valid numerals below; the
       primitive's own range check is the final backstop).

    Empty strings pass through untouched. Callers filter those out before
    ever reaching this function (see ``_fpe_value``); this is the leaf
    permutation only.
    """
    n = len(s)
    if n == 0:
        return s
    radix = len(charset)

    if len(key) != _ff1.FF1_KEY_BYTES:
        raise FpeUnencryptableError(
            f"fpe key must be exactly {_ff1.FF1_KEY_BYTES} bytes (AES-256 only); "
            f"got {len(key)}. This indicates a key-derivation bug, not a data issue.",
            code="fpe.unencryptable_length",
        )
    if not (_ff1.FF1_MIN_RADIX <= radix <= _ff1.FF1_MAX_RADIX):
        raise FpeUnencryptableError(
            f"fpe charset resolves to radix {radix}, outside the deployed profile's "
            f"[{_ff1.FF1_MIN_RADIX}, {_ff1.FF1_MAX_RADIX}] range.",
            code="fpe.unencryptable_length",
        )
    if len(set(charset)) != radix:
        raise FpeUnencryptableError(
            "fpe charset contains duplicate symbols; FF1 requires an ordered, "
            "duplicate-free alphabet (a duplicate would make decode ambiguous).",
            code="fpe.unencryptable_length",
        )
    check_charset_ascii_printable(charset)
    min_len = _ff1.min_domain_length(radix)
    if n < min_len:
        raise FpeUnencryptableError(
            f"value has {n} in-charset character(s); the domain (radix**length = "
            f"{radix}**{n} = {radix**n}) is below the FF1 minimum admissible domain "
            f"({_ff1.FF1_MIN_DOMAIN}). Use a wider charset, a longer/covering format, "
            "or route this column through a different strategy.",
            code="fpe.unencryptable_domain",
        )
    if n > _ff1.FF1_MAX_LEN:
        raise FpeUnencryptableError(
            f"value has {n} in-charset characters, exceeding the deployed FF1 "
            f"profile's maximum length of {_ff1.FF1_MAX_LEN}.",
            code="fpe.unencryptable_length",
        )
    if len(tweak) > _ff1.FF1_MAX_TWEAK_LEN:
        raise FpeUnencryptableError(
            f"fpe tweak is {len(tweak)} bytes, exceeding the deployed profile's "
            f"maximum tweak length ({_ff1.FF1_MAX_TWEAK_LEN} bytes).",
            code="fpe.unencryptable_length",
        )

    char_to_idx = _char_lookup(charset)
    # P7: check membership before indexing rather than catching the dict
    # KeyError. A KeyError raised from `char_to_idx[ch]` carries the actual
    # offending character as its arg, and Python sets that KeyError as the
    # new exception's `__context__` even when raised with `from None` or
    # after explicitly clearing the attribute -- the interpreter re-attaches
    # it at raise time. Raising outside any `except` block sidesteps this
    # entirely, so the source character never reaches the exception chain.
    if any(ch not in char_to_idx for ch in s):
        raise FpeUnencryptableError(
            "fpe body contains a character outside the configured alphabet; "
            "every body character must be a member of the resolved charset.",
            code="fpe.unencryptable_length",
        )
    numerals = [char_to_idx[ch] for ch in s]

    try:
        result = (
            _ff1.encrypt(key, tweak, radix, numerals)
            if forward
            else _ff1.decrypt(key, tweak, radix, numerals)
        )
    except _ff1.Ff1Error as exc:
        raise FpeUnencryptableError(
            f"FF1 rejected this input: {exc}", code="fpe.unencryptable_length"
        ) from exc
    return "".join(charset[i] for i in result)


def _fpe_pure_value(
    s: str,
    key: bytes,
    charset: str,
    tweak: bytes,
    validate_luhn: bool,
    *,
    forward: bool,
    checksum: str | None = None,
) -> str:
    """FF1-encrypt (or invert) a string consisting entirely of charset characters.

    Checksum mode (``checksum`` is not None): permutes the non-check-digit
    portion of the string and recomputes the check digit from the encrypted
    body.  Output is checksum-valid by construction in both the forward and
    inverse directions (see ``_fpe_checksum_permute``).  Checksum mode takes
    priority over ``validate_luhn`` when both are set.

    Luhn mode (``validate_luhn=True``, no ``checksum``): permutes the BODY
    (all chars but the last) and appends the Luhn check digit of the result,
    in both directions. Encrypt output is Luhn-valid by construction; decrypt
    restores the body exactly and recomputes the check digit, so a Luhn-valid
    source (the domain the mode exists for: PANs) round-trips byte-exactly.
    The pre-WS1 shape (permute all n chars, overwrite the last with the check
    digit) discarded one encrypted character and was therefore not invertible;
    the change is covered by the SEED_PROTOCOL_VERSION 4 -> 5 bump."""
    # Defense-in-depth only: `_fpe_value` (this function's only caller) now
    # rejects an empty value with `FpeUnencryptableError` before ever reaching
    # here (plan P2), so `s` is unreachable-empty on both its call sites --
    # empty passed straight through, empty out-of-charset positions raised
    # earlier, and the non-preserve-separators path already required `val`
    # itself non-empty. Kept as a guard against a hypothetical future direct
    # caller of this "pure" leaf, not as documented empty-string policy.
    if not s:
        return s
    if checksum is not None:
        # Codex cross-model review (2026-07-14): route EVERY length through the
        # checksum path (was `and len(s) >= 2`). A len-0/1 value used to skip this
        # dispatch, fall to `_permute`, and permute to itself -- bypassing the
        # scheme's length/validity check entirely. `_fpe_checksum_permute` now
        # length-validates up front and fails closed on any invalid length.
        # Lazy import breaks the fpe <-> _fpe_checksum cycle: _fpe_checksum imports
        # `_permute` from this module at load, so it must load AFTER fpe is ready.
        from decoy_engine.transforms._fpe_checksum import _fpe_checksum_permute

        return _fpe_checksum_permute(s, key, charset, tweak, checksum, forward=forward)
    if validate_luhn and len(s) >= 2:
        body = _permute(s[:-1], key, charset, tweak, forward=forward)
        return body + _luhn_check_digit(body)
    return _permute(s, key, charset, tweak, forward=forward)


def _fpe_value(
    val: str,
    key: bytes,
    charset: str,
    tweak: bytes,
    preserve_separators: bool,
    validate_luhn: bool,
    checksum: str | None = None,
    *,
    forward: bool,
) -> str:
    """Shared encrypt/decrypt orchestration over one value.

    DE-01 cluster-C (2026-07-14) closes two silent-failure paths at the source:

    - preserve_separators=True with ZERO in-charset characters (an
      all-out-of-charset value, e.g. a fully non-ASCII name or an orphan key
      whose every character is outside the charset). The pre-fix covering-hash
      fallback produced an in-charset value the inverse cipher cannot recover
      (verified non-round-trip); there is nothing to format-preserving-encrypt,
      so fail closed (`FpeUnencryptableError`) rather than emit a value that
      silently will not reverse.
    - preserve_separators=False with any out-of-charset character. The pre-fix
      path returned the whole value UNCHANGED (a silent cleartext no-op on the
      executed V2 path); fail closed instead.

    The PARTIAL case (some in-charset chars, some out-of-charset) under
    preserve_separators=True is intentionally UNCHANGED: the in-charset content
    is permuted and out-of-charset characters (format separators or prefixes)
    are reinserted verbatim. The residual partial-plaintext disclosure a format
    prefix carries is surfaced by the handler as a `QualityWarning`; full
    coverage is a further follow-up (structured/typed-subfield FPE), independent
    of the FF1 primitive swap (Task 5.2)."""
    # Empty-string reject (plan P2): a present-but-empty value is NOT a null --
    # nulls never reach this layer at all (skipped via na_mask one layer up) --
    # so an empty string here is a genuine zero-length value with a zero-length
    # (radix**0 == 1) in-charset domain, below the FF1 minimum admissible domain
    # exactly like any other sub-floor value. The engine used to pass this
    # through unchanged (a silent cleartext no-op distinct from every other
    # unencryptable case below, which all fail closed); that carve-out is gone.
    if not val:
        raise FpeUnencryptableError(
            "fpe cannot encrypt an empty (zero-length) value: its in-charset "
            "domain is below the FF1 minimum admissible domain. An empty "
            "string is not a null (nulls never reach this layer) and carries "
            "no format-preserving domain to permute over.",
            value=val,
            code="fpe.unencryptable_domain",
        )
    charset_set = set(charset)
    if preserve_separators:
        positions = [i for i, ch in enumerate(val) if ch in charset_set]
        if not positions:
            raise FpeUnencryptableError(
                f"fpe cannot encrypt this {len(val)}-character value: it has no "
                "character in the configured charset, so there is nothing to "
                "format-preserving-encrypt and the value cannot be reversibly "
                "masked. The engine fails closed rather than emit a "
                "non-invertible covering hash that would silently not "
                "round-trip. Use a charset that covers this column's data, or route "
                "the column through a different strategy (hash/redact).",
                value=val,
            )
        body = _fpe_pure_value(
            "".join(val[i] for i in positions),
            key,
            charset,
            tweak,
            validate_luhn,
            forward=forward,
            checksum=checksum,
        )
        # Codex cross-model review (2026-07-14): the permuted body must have exactly
        # one character per in-charset position. It always does after the checksum
        # length-validation (a fixed-width scheme body used to be shorter/longer than
        # the source, which the old non-strict zip silently absorbed -- leaking the
        # surplus source char). This guard turns any future mismatch into a loud
        # fail-closed error instead of a silent leak/truncation; the `strict` zip is
        # the belt-and-suspenders backstop.
        if len(body) != len(positions):
            raise FpeUnencryptableError(
                f"fpe internal length invariant: permuted body length {len(body)} != "
                f"{len(positions)} in-charset positions (source value length "
                f"{len(val)}). Refusing to reinsert a mismatched body, which would "
                "leak or drop a character.",
                value=val,
            )
        result = list(val)
        for pos, ch in zip(positions, body, strict=True):
            result[pos] = ch
        return "".join(result)
    if not all(ch in charset_set for ch in val):
        out_of_charset_count = len({ch for ch in val if ch not in charset_set})
        raise FpeUnencryptableError(
            f"fpe cannot encrypt this {len(val)}-character value with "
            f"preserve_separators=false: it contains {out_of_charset_count} distinct "
            "out-of-charset character(s). Returning the value unchanged would leak it "
            "in the clear, so the engine fails closed. Enable preserve_separators to "
            "keep structural separators in place, use a charset that covers the data, "
            "or route the column through a different strategy.",
            value=val,
        )
    return _fpe_pure_value(
        val, key, charset, tweak, validate_luhn, forward=forward, checksum=checksum
    )


def fpe_encrypt_value(
    val: str,
    key: bytes,
    charset: str,
    tweak: bytes,
    preserve_separators: bool = True,
    validate_luhn: bool = False,
    checksum: str | None = None,
) -> str:
    """Encrypt one value with the keyed format-preserving permutation.

    When ``checksum`` is supplied the non-check-digit portion is permuted and
    the correct check digit is appended / inserted, making the output
    checksum-valid by construction.  Supported schemes: ``'luhn'``,
    ``'npi'``, ``'iban'``, ``'vin'``, ``'isbn13'``, ``'ean13'``,
    ``'gtin'`` (see ``decoy_engine.checksums``).

    ``checksum`` takes priority over ``validate_luhn`` when both are set.
    """
    return _fpe_value(
        val, key, charset, tweak, preserve_separators, validate_luhn, checksum, forward=True
    )


def fpe_decrypt_value(
    val: str,
    key: bytes,
    charset: str,
    tweak: bytes,
    preserve_separators: bool = True,
    validate_luhn: bool = False,
    checksum: str | None = None,
) -> str:
    """Invert ``fpe_encrypt_value`` under the same (key, charset, tweak, config).

    With ``validate_luhn=True`` or ``checksum`` set, the check digit is
    recomputed rather than stored, so the round-trip is exact iff the source
    was already checksum-valid for the configured scheme (see
    ``_fpe_pure_value`` and ``_fpe_checksum_permute``).
    """
    return _fpe_value(
        val, key, charset, tweak, preserve_separators, validate_luhn, checksum, forward=False
    )
