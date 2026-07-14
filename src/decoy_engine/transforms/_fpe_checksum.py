"""Checksum-scheme body permutation for the FPE mask strategy.

Split out of ``transforms/fpe.py`` (DE-01 cluster-C, 2026-07-14) so the cipher
module drops back under the 600-LOC cap after the fail-closed short-value raises
were added. This is the cohesive "structured-identifier checksum handling"
concern: it permutes the non-check-digit portion of a value and recomputes the
scheme's check digit so the output is valid-by-construction.

It reuses the low-level `_permute` primitive from ``transforms.fpe``; that import
is module-level but the module itself is only imported lazily (from inside
``fpe._fpe_pure_value``), so ``fpe`` is always fully loaded first and no import
cycle forms.
"""

from __future__ import annotations

from decoy_engine.errors import FpeChecksumError
from decoy_engine.transforms.fpe import _permute

# Valid length(s) per checksum scheme. NPI/ISBN-13/EAN-13/VIN have EXACT lengths;
# GTIN has four legal lengths (GS1); Luhn is variable (any Luhn-protected digit
# string) so only a floor applies. Codex cross-model review (2026-07-14): a value
# outside the scheme's valid length used to either bypass the check (len 0/1
# skipped the >=2 dispatch guard and permuted to itself) or, when over-length,
# leak the extra source digit raw (separator-preserve) or truncate it
# (preserve_separators=false), because the checksum body is a FIXED width and the
# surplus never round-trips. Fail closed on any invalid length instead.
_EXACT_LENGTHS: dict[str, frozenset[int]] = {
    "npi": frozenset({10}),
    "isbn13": frozenset({13}),
    "ean13": frozenset({13}),
    "vin": frozenset({17}),
    "gtin": frozenset({8, 12, 13, 14}),
}
# Luhn is length-agnostic beyond needing a body plus a check digit.
_MIN_LENGTHS: dict[str, int] = {"luhn": 2}


def _validate_scheme_length(scheme: str, s: str) -> None:
    """Fail closed unless ``s`` has a valid length for ``scheme``.

    Raised BEFORE any permutation so an invalid-length value never (a) bypasses
    the checksum path and permutes to itself (len 0/1), nor (b) leaks/truncates a
    surplus character against the scheme's fixed-width body (over-length).
    """
    exact = _EXACT_LENGTHS.get(scheme)
    if exact is not None and len(s) not in exact:
        expected = " or ".join(str(n) for n in sorted(exact))
        raise FpeChecksumError(
            f"FPE checksum scheme {scheme!r} requires exactly {expected} character(s); "
            f"got {len(s)} for {s!r}. A value outside the scheme's fixed length cannot "
            "be format-preserving-encrypted without leaking or truncating a character, "
            "so the engine fails closed. Fix the source data or route this column "
            "through a different strategy.",
            scheme=scheme,
        )
    minimum = _MIN_LENGTHS.get(scheme)
    if minimum is not None and len(s) < minimum:
        raise FpeChecksumError(
            f"FPE checksum scheme {scheme!r} requires at least {minimum} characters "
            f"(body + check digit); got {len(s)} for {s!r}. The engine fails closed "
            "rather than pass the value through unmasked.",
            scheme=scheme,
        )


def _fpe_checksum_permute(
    s: str,
    key: bytes,
    charset: str,
    tweak: bytes,
    scheme: str,
    *,
    forward: bool,
) -> str:
    """Permute a string and recompute the check digit for the given scheme.

    Each scheme enforces its own alphabet constraint so the output is
    valid-by-construction and calc_check_digit never KeyErrors.

    Per-scheme rules
    ----------------
    luhn / ean13 / gtin
        Digit-only schemes.  The permutation charset is constrained to
        ``'0123456789'`` regardless of the caller's charset config.  Check
        digit appended at the end.

    npi
        NPI (CMS NPPES, 2008): first digit must be 1 or 2.  The leading
        digit is pinned (preserved from the source), and only the 8-digit
        middle body is permuted over digits.  Check digit appended at end.
        Minimum length: 10 chars (9-char body + 1-char check).

    isbn13
        The 3-char bookland prefix (978 or 979) is pinned; the 9-digit
        inner body (s[3:12]) is permuted over digits; the EAN-13 check
        digit is appended.  Minimum length: 13 chars.

    vin
        The permutation charset is constrained to the VIN alphabet
        (0-9 A-Z excluding I, O, Q -- NHTSA 49 CFR Part 565).  Any I/O/Q
        in the caller's charset is silently dropped; the body characters
        are translated to the nearest VIN-legal value (already ensured by
        the permutation staying in-alphabet).  16 non-check chars (s[:8] +
        s[9:]) are permuted; the ISO 3779 check char is inserted at pos 8.
        Minimum length: 17 chars.

    iban
        FAILS CLOSED.  Per-country BBAN structure (enforced by
        stdnum.iban.validate) cannot be satisfied by a free Feistel
        permutation.  Raises ``FpeChecksumError`` unconditionally.
        Use validate-only or a different strategy for IBAN columns.

    unknown scheme
        FAILS CLOSED.  Any scheme name not in ``checksums._KNOWN_SCHEMES``
        raises ``FpeChecksumError``.  The pre-SP04 fall-through to plain
        FPE with no check digit was silent misconfiguration; Decoy forbids
        that pattern.

    Too-short values
        FAIL CLOSED (DE-01 cluster-C).  A value below a scheme's minimum
        length (npi<10, isbn13<13, vin<17) used to ``return s`` UNCHANGED --
        a silent cleartext pass-through of an identifier the config asked to
        mask.  Each now raises ``FpeChecksumError`` instead.

    The function is symmetric: the same body permutation runs in both the
    forward (encrypt) and inverse (decrypt) directions.
    """
    from decoy_engine.checksums import _KNOWN_SCHEMES, calc_check_digit

    # H2: unknown scheme - fail closed.
    if scheme not in _KNOWN_SCHEMES:
        raise FpeChecksumError(
            f"FPE checksum mode received unknown scheme {scheme!r}. "
            f"Known schemes: {sorted(_KNOWN_SCHEMES)}. "
            "Check your config for typos; valid fpe checksum schemes are "
            "luhn, npi, vin, isbn13, ean13, gtin (iban is not supported for FPE).",
            scheme=scheme,
        )

    # B1: IBAN - fail closed.
    if scheme == "iban":
        raise FpeChecksumError(
            "FPE checksum mode does not support 'iban': per-country BBAN "
            "structure (enforced by stdnum.iban.validate) cannot be satisfied "
            "by a format-preservation permutation. Use validate-only or a "
            "different strategy for IBAN columns. "
            "See carry-forward note in p5-infra-1-python-stdnum.md.",
            scheme="iban",
        )

    # Codex cross-model review (2026-07-14): validate length up front for EVERY
    # scheme, before any permutation. This closes both the len-0/1 bypass (a
    # single char skipped the old per-branch `len < N` floors and permuted to
    # itself) and the over-length leak/truncation (a surplus char never fit the
    # fixed-width checksum body).
    _validate_scheme_length(scheme, s)

    _DIGITS_ONLY = "0123456789"

    # luhn / ean13 / gtin: digit-only, check digit at end.
    if scheme in ("luhn", "ean13", "gtin"):
        body = _permute(s[:-1], key, _DIGITS_ONLY, tweak, forward=forward)
        return body + calc_check_digit(scheme, body)

    # M1: NPI - pin leading digit, permute 8-digit middle body over digits.
    if scheme == "npi":
        # Pin the first character (must be 1 or 2 per NPPES).
        leading = s[0]
        middle_body = _permute(s[1:9], key, _DIGITS_ONLY, tweak, forward=forward)
        body9 = leading + middle_body
        return body9 + calc_check_digit("npi", body9)

    # B2: isbn13 - pin bookland prefix (s[:3]), permute 9 inner digits.
    if scheme == "isbn13":
        prefix = s[:3]  # '978' or '979' -- pinned
        inner = _permute(s[3:12], key, _DIGITS_ONLY, tweak, forward=forward)
        body12 = prefix + inner
        return body12 + calc_check_digit("isbn13", body12)

    # H1: VIN - constrain charset to VIN alphabet, permute 16-char body.
    if scheme == "vin":
        # Drop I, O, Q from whatever charset the caller provided.
        from decoy_engine.checksums import _VIN_CHARSET

        vin_charset = "".join(c for c in charset if c in _VIN_CHARSET)
        if len(vin_charset) < 2:
            # Fallback: use the canonical VIN digit subset so permutation can proceed.
            vin_charset = _DIGITS_ONLY
        raw_body = s[:8] + s[9:]  # 16 non-check chars
        enc = _permute(raw_body, key, vin_charset, tweak, forward=forward)
        check = calc_check_digit("vin", enc)
        return enc[:8] + check + enc[8:]

    # Should never reach here: all known non-IBAN schemes handled above.
    raise FpeChecksumError(
        f"Unhandled scheme {scheme!r} in _fpe_checksum_permute (internal error).",
        scheme=scheme,
    )
