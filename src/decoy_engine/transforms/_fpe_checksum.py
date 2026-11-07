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

``keep_bin`` (Luhn mode only) keeps a payment card's issuer prefix, its BIN or the
ISO/IEC 7812-1:2017 8-digit IIN, in the clear and FF1-encrypts only the account
digits between it and the check digit, the "preserve starting digits" pattern of
Delphix's Payment Card framework and Protegrity's left-clear setting. The kept
prefix is appended to the FF1 tweak, following NIST SP 800-38G's advice to use
data statically associated with the plaintext (here the clear digits) as the
tweak (NIST ITL Bulletin 2016-04 works this exact card example). This module is
the only reader of the ``keep_bin`` config key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from decoy_engine import reasons
from decoy_engine.errors import FpeChecksumError, FpeUnencryptableError
from decoy_engine.transforms import _ff1
from decoy_engine.transforms.fpe import _permute, resolve_fpe_charset

_KEEP_BIN: Final = "keep_bin"
KEEP_BIN_LENGTHS: Final[frozenset[int]] = frozenset({6, 8})
# ISO/IEC 7812-1: a PAN has at most 19 digits.
PAN_MAX_DIGITS: Final = 19
_DIGITS: Final = "0123456789"

KeepBinProblem = Literal["invalid", "requires_luhn"]


def keep_bin_problem(cfg: Mapping[str, Any]) -> KeepBinProblem | None:
    """Why a provider config's ``keep_bin`` cannot be honoured, or None."""
    return keep_bin_fields_problem(
        cfg.get(_KEEP_BIN), cfg.get("checksum"), cfg.get("charset", "digits")
    )


def keep_bin_fields_problem(value: Any, checksum: Any, charset_spec: Any) -> KeepBinProblem | None:
    """``keep_bin_problem`` over already-extracted fields (for config carriers).

    Only None means off. Anything else must be exactly the int 6 or 8 (a bool is
    refused even though it is an int) on ``checksum: luhn`` with the ``digits``
    charset, since the kept prefix is "the first k digits".
    """
    if value is None:
        return None
    if type(value) is not int or value not in KEEP_BIN_LENGTHS:
        return "invalid"
    if checksum != "luhn":
        return "requires_luhn"
    if not isinstance(charset_spec, str) or resolve_fpe_charset(charset_spec) != _DIGITS:
        return "requires_luhn"
    return None


_PROBLEM_TEXT: Final[dict[str, str]] = {
    "invalid": "fpe keep_bin must be the integer 6 or 8 (or absent/null for off).",
    "requires_luhn": (
        "fpe keep_bin needs checksum: luhn and the digits charset; the kept prefix is the "
        "first digits of a Luhn-checked card number."
    ),
}


def keep_bin_problem_text(problem: KeepBinProblem) -> str:
    """The value-free explanation of a ``keep_bin_problem`` result."""
    return _PROBLEM_TEXT[problem]


def keep_bin_error(problem: KeepBinProblem) -> FpeChecksumError:
    """The value-free error for a ``keep_bin_problem`` result (returned, not raised)."""
    return FpeChecksumError(keep_bin_problem_text(problem), scheme="luhn")


def keep_bin_value(cfg: Mapping[str, Any]) -> int:
    """The ``keep_bin`` a value call takes: 0 for off, else 6 or 8.

    Raises a value-free ``FpeChecksumError`` when the config cannot honour it.
    """
    problem = keep_bin_problem(cfg)
    if problem is not None:
        raise keep_bin_error(problem)
    value = cfg.get(_KEEP_BIN)
    return 0 if value is None else int(value)


def raw_keep_bin(cfg: Mapping[str, Any]) -> Any:
    """The unvalidated ``keep_bin`` (None for absent or null), for config carriers
    that validate later (``FpeConfig._resolve``)."""
    return cfg.get(_KEEP_BIN)


def check_keep_bin_call(keep_bin: int, checksum: str | None) -> None:
    """Value-layer backstop: refuse a ``keep_bin`` the value call cannot honour.

    Every encrypt and decrypt passes through here, so a caller that skipped plan
    compile (or built a config by hand) never has ``keep_bin`` silently ignored.
    """
    if type(keep_bin) is not int or (keep_bin != 0 and keep_bin not in KEEP_BIN_LENGTHS):
        raise FpeChecksumError(keep_bin_problem_text("invalid"), scheme=checksum)
    if keep_bin and checksum != "luhn":
        raise FpeChecksumError(keep_bin_problem_text("requires_luhn"), scheme=checksum)


def _check_keep_bin_shape(s: str, keep_bin: int) -> None:
    """Refuse a value that is not a PAN, or leaves FF1 too few digits to encrypt.

    The floor itself is enforced again by ``_permute``; this check only words the
    failure in ``keep_bin`` terms. Messages carry counts only.
    """
    if len(s) > PAN_MAX_DIGITS:
        raise FpeChecksumError(
            f"FPE checksum scheme 'luhn' with keep_bin requires at most {PAN_MAX_DIGITS} "
            f"digits (an ISO/IEC 7812 PAN); got {len(s)}. The engine fails closed.",
            scheme="luhn",
        )
    encrypted = len(s) - keep_bin - 1
    floor = _ff1.min_domain_length(len(_DIGITS))
    if encrypted < floor:
        raise FpeUnencryptableError(
            f"keep_bin={keep_bin} on a {len(s)}-digit value leaves {encrypted} digit(s) to "
            f"encrypt; FF1 needs at least {floor} (domain 10**{floor} = "
            f"{_ff1.FF1_MIN_DOMAIN}). Use keep_bin 6, or another strategy for this column.",
            code=reasons.runtime.FPE__UNENCRYPTABLE_DOMAIN,
        )


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
            f"got {len(s)}. A value outside the scheme's fixed length cannot "
            "be format-preserving-encrypted without leaking or truncating a character, "
            "so the engine fails closed. Fix the source data or route this column "
            "through a different strategy.",
            scheme=scheme,
        )
    minimum = _MIN_LENGTHS.get(scheme)
    if minimum is not None and len(s) < minimum:
        raise FpeChecksumError(
            f"FPE checksum scheme {scheme!r} requires at least {minimum} characters "
            f"(body + check digit); got {len(s)}. The engine fails closed "
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
    keep_bin: int = 0,
) -> str:
    """Permute a string and recompute the check digit for the given scheme.

    Each scheme enforces its own alphabet constraint so the output is
    valid-by-construction and calc_check_digit never KeyErrors.

    Per-scheme rules
    ----------------
    luhn with keep_bin (6 or 8)
        The first ``keep_bin`` digits are kept, the digits up to the check
        digit are permuted with the prefix appended to the tweak, and the Luhn
        digit is recomputed. At most 19 digits; at least ``keep_bin + 7`` (the
        FF1 floor needs six encrypted digits). The caller (``_fpe_value``) has
        already refused ``keep_bin`` on any other scheme.

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
        stdnum.iban.validate) cannot be satisfied by a free
        format-preservation permutation.  Raises ``FpeChecksumError`` unconditionally.
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

    Invalid source checksum (Task 5.2 plan P5-final)
        On the FORWARD (encrypt) direction only, the complete normalized
        identifier, INCLUDING its check digit, must already satisfy
        ``checksums.validate()`` before any permutation runs. A value whose
        stated check digit does not match its body is not a valid instance
        of the scheme (e.g. an SSN typed in the PAN column with a mangled
        Luhn digit); permuting it anyway would produce output that looks
        checksum-valid but has no honest source, so the engine fails closed
        with ``FpeChecksumError`` (code ``fpe.checksum_invalid_source``)
        instead. Decrypt does NOT re-validate: the ciphertext's check digit
        is recomputed from the decrypted body, not read from the ciphertext,
        so there is nothing meaningful to validate on that side.

    The function is symmetric: the same body permutation runs in both the
    forward (encrypt) and inverse (decrypt) directions.
    """
    try:
        from decoy_engine.checksums import _KNOWN_SCHEMES, calc_check_digit, validate

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
        check_keep_bin_call(keep_bin, scheme)
        _validate_scheme_length(scheme, s)
        if keep_bin:
            _check_keep_bin_shape(s, keep_bin)

        # P5-final: validate the COMPLETE source identifier (check digit included)
        # before permuting, forward direction only. `s` here has already had any
        # separators stripped by the caller (`_fpe_value`'s in-charset extraction),
        # which every supported scheme's check-digit arithmetic is indifferent to.
        if forward and not validate(scheme, s):
            raise FpeChecksumError(
                f"FPE checksum mode: source value fails checksums.validate() for "
                f"scheme {scheme!r}; its check digit does not match its body. "
                "Permuting an already-invalid identifier would produce output "
                "that looks checksum-valid but has no honest source, so the "
                "engine fails closed. Fix the source data or route this column "
                "through a different strategy.",
                scheme=scheme,
                code=reasons.runtime.FPE__CHECKSUM_INVALID_SOURCE,
            )

        _DIGITS_ONLY = "0123456789"

        # keep_bin: the issuer prefix stays and is bound into the tweak; the account
        # digits up to the check digit are permuted; the Luhn digit is recomputed.
        if keep_bin:
            prefix = s[:keep_bin]
            tweak_with_prefix = tweak + prefix.encode("ascii")
            account = _permute(s[keep_bin:-1], key, _DIGITS, tweak_with_prefix, forward=forward)
            return prefix + account + calc_check_digit("luhn", prefix + account)

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
    except BaseException:
        del key
        raise
