"""
Format-Preserving Encryption (FPE) mask strategy.

Replaces each string value with another string of the same length over the
same character set. Same input + same key produces the same output across
runs and instances (keyed determinism via the existing derive_key path,
identical to HashStrategy and DateShiftStrategy).

Algorithm: 8-round type-II Feistel permutation over Z_(r^u) x Z_(r^v) where
n = u + v = input length and r = |charset|, using HMAC-SHA256 as the round
function. The Feistel construction is a bijection regardless of the round
function (odd rounds shift B keyed on A; even rounds shift A keyed on B),
so no two inputs map to the same output. Requires only stdlib (no new
package dependency added).

Pattern: Type-II Feistel + HMAC-SHA256 (Feistel 1973; HMAC RFC 2104).
  Feistel: original construction by Horst Feistel at IBM (1973).
  HMAC: https://datatracker.ietf.org/doc/html/rfc2104

Design note: this is not NIST FF1 (which requires AES-CBC and therefore
the `cryptography` package). The Feistel+HMAC approach has the same
user-visible properties (format-preserving, bijective, keyed-deterministic)
using the HMAC-SHA256 primitive that is already in every other keyed
transform. Defer a hard AES dep until a customer asks for NIST SP 800-38G
compliance by name.
"""

import hashlib
import hmac
import struct
from typing import Any

import pandas as pd

from decoy_engine.errors import MaskKeyDerivationError
from decoy_engine.transforms.base import BaseMaskingStrategy

_CHARSETS: dict[str, str] = {
    "digits": "0123456789",
    "alpha": "abcdefghijklmnopqrstuvwxyz",
    "ALPHA": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "alphanum": "0123456789abcdefghijklmnopqrstuvwxyz",
    "ALPHANUM": "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
}

# F5 fix: pre-computed {char: index} lookup per charset so _encode is O(n)
# per string instead of O(n * r) (where r = |charset|). For 1M rows of 9-digit
# values over ALPHANUM (r=62), this drops the inner loop from 558M operations
# to 9M. Keyed on the charset STRING (not the name) so _fpe_pure can look
# up without a reverse mapping.
_CHARSET_INDEX: dict[str, dict[str, int]] = {
    chars: {ch: i for i, ch in enumerate(chars)} for chars in _CHARSETS.values()
}

_ROUNDS = 8  # Feistel rounds; 8 gives good pseudorandomness with negligible overhead


def _prf(key: bytes, round_index: int, tweak: bytes, operand: int) -> bytes:
    """HMAC-SHA256 round function: keyed on (round_index, tweak, operand)."""
    operand_b = operand.to_bytes(max((operand.bit_length() + 7) // 8, 1), "big")
    msg = struct.pack(">B", round_index) + tweak + b"\xff" + operand_b
    return hmac.new(key, msg, hashlib.sha256).digest()


def _feistel(key: bytes, tweak: bytes, x: int, u_mod: int, v_mod: int) -> int:
    """8-round type-II Feistel permutation over Z_u_mod × Z_v_mod.

    Even round i: A = (A + PRF(key, i, tweak, B)) mod u_mod
    Odd  round i: B = (B + PRF(key, i, tweak, A)) mod v_mod

    This is a bijection over [0, u_mod * v_mod) regardless of the PRF.
    Input x must be in [0, u_mod * v_mod).
    """
    A, B = divmod(x, v_mod)  # A ∈ [0, u_mod), B ∈ [0, v_mod)
    for i in range(_ROUNDS):
        if i % 2 == 0:
            F = int.from_bytes(_prf(key, i, tweak, B), "big") % u_mod
            A = (A + F) % u_mod
        else:
            F = int.from_bytes(_prf(key, i, tweak, A), "big") % v_mod
            B = (B + F) % v_mod
    return A * v_mod + B


def _feistel_inverse(key: bytes, tweak: bytes, y: int, u_mod: int, v_mod: int) -> int:
    """Inverse of `_feistel`: undo the rounds in reverse order.

    Each round only modifies one half using the OTHER half as the PRF
    operand, so at undo time the operand still holds the value it had
    when the round was applied; subtracting the same PRF output mod the
    same modulus restores the half exactly (additive Feistel inversion,
    the textbook construction property; Feistel 1973)."""
    A, B = divmod(y, v_mod)
    for i in reversed(range(_ROUNDS)):
        if i % 2 == 0:
            F = int.from_bytes(_prf(key, i, tweak, B), "big") % u_mod
            A = (A - F) % u_mod
        else:
            F = int.from_bytes(_prf(key, i, tweak, A), "big") % v_mod
            B = (B - F) % v_mod
    return A * v_mod + B


def _encode(s: str, charset: str, char_to_idx: dict[str, int] | None = None) -> int:
    """Encode a string over `charset` as a base-r integer.

    F5 fix: when ``char_to_idx`` is provided (the pre-computed {char: index}
    lookup), use it for O(1) character indexing. Falls back to O(r)
    ``charset.index`` when not provided, preserving the original signature
    for any out-of-tree caller.
    """
    r = len(charset)
    x = 0
    if char_to_idx is None:
        for ch in s:
            x = x * r + charset.index(ch)
    else:
        for ch in s:
            x = x * r + char_to_idx[ch]
    return x


def _decode(x: int, charset: str, length: int) -> str:
    """Decode a base-r integer back to a string of `length` characters."""
    r = len(charset)
    digits = []
    for _ in range(length):
        digits.append(charset[x % r])
        x //= r
    return "".join(reversed(digits))


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


def _char_lookup(charset: str) -> dict[str, int]:
    # F5 fix: pre-computed {char: index} lookup (built once per charset at
    # module load) replaces O(r) charset.index() per character; per-call
    # dict for custom charsets keeps O(n) instead of O(n*r).
    lookup = _CHARSET_INDEX.get(charset)
    if lookup is None:
        lookup = {ch: i for i, ch in enumerate(charset)}
    return lookup


def _single_char_shift(key: bytes, tweak: bytes) -> int:
    """Keyed rotation amount for the degenerate single-character case.

    QA-10 F2 (2026-06-01): the shift depends on (key, tweak) only, NOT on
    the source character, so the map is a uniform alphabet rotation
    (trivially bijective; see the original fix note)."""
    msg = b"fpe-single\xff" + tweak
    return int.from_bytes(hmac.new(key, msg, hashlib.sha256).digest(), "big")


def _covering_hash_to_charset(val: str, key: bytes, charset: str, tweak: bytes) -> str:
    """In-charset cover for an all-out-of-charset value (fix #42).
    Per-position keyed PRF: HMAC-SHA256 (RFC 2104), HKDF-style domain sep (RFC 5869).
    Deterministic under (key, charset, tweak, val); output never equals val."""
    if not val:
        return val
    r, dom, val_b = len(charset), b"covering\xff", val.encode("utf-8", errors="replace")
    out: list[str] = []
    for i in range(len(val)):
        msg = dom + struct.pack(">I", i) + b"\xff" + tweak + b"\xff" + val_b
        out.append(charset[int.from_bytes(hmac.new(key, msg, hashlib.sha256).digest(), "big") % r])
    return "".join(out)


def _permute(s: str, key: bytes, charset: str, tweak: bytes, *, forward: bool) -> str:
    """Feistel-permute (or invert) a string made entirely of charset chars."""
    n = len(s)
    if n == 0:
        return s
    if n == 1:
        idx = charset.index(s[0])
        F = _single_char_shift(key, tweak)
        shift = F if forward else -F
        return charset[(idx + shift) % len(charset)]
    u = (n + 1) // 2  # ceil(n/2)
    v = n - u  # floor(n/2)
    u_mod = len(charset) ** u
    v_mod = len(charset) ** v
    x = _encode(s, charset, _char_lookup(charset))
    fn = _feistel if forward else _feistel_inverse
    y = fn(key, tweak, x, u_mod, v_mod)
    return _decode(y, charset, n)


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

    The function is symmetric: the same body permutation runs in both the
    forward (encrypt) and inverse (decrypt) directions.
    """
    from decoy_engine.checksums import _KNOWN_SCHEMES, calc_check_digit
    from decoy_engine.errors import FpeChecksumError

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

    _DIGITS_ONLY = "0123456789"

    # luhn / ean13 / gtin: digit-only, check digit at end.
    if scheme in ("luhn", "ean13", "gtin"):
        body = _permute(s[:-1], key, _DIGITS_ONLY, tweak, forward=forward)
        return body + calc_check_digit(scheme, body)

    # M1: NPI - pin leading digit, permute 8-digit middle body over digits.
    if scheme == "npi":
        # L1: NPI needs at least 10 chars (9-char body + check); pass through if too short.
        if len(s) < 10:
            return s
        # Pin the first character (must be 1 or 2 per NPPES).
        leading = s[0]
        middle_body = _permute(s[1:9], key, _DIGITS_ONLY, tweak, forward=forward)
        body9 = leading + middle_body
        return body9 + calc_check_digit("npi", body9)

    # B2: isbn13 - pin bookland prefix (s[:3]), permute 9 inner digits.
    if scheme == "isbn13":
        # L1: isbn13 needs 13 chars; pass through if too short.
        if len(s) < 13:
            return s
        prefix = s[:3]  # '978' or '979' -- pinned
        inner = _permute(s[3:12], key, _DIGITS_ONLY, tweak, forward=forward)
        body12 = prefix + inner
        return body12 + calc_check_digit("isbn13", body12)

    # H1: VIN - constrain charset to VIN alphabet, permute 16-char body.
    if scheme == "vin":
        # L1: VIN needs 17 chars; pass through if too short.
        if len(s) < 17:
            return s
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
    """FPE (or invert) a string consisting entirely of charset characters.

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
    if checksum is not None and len(s) >= 2:
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

    With preserve_separators=True: charset chars are extracted, permuted, and
    reinserted; zero in-charset chars invoke the covering hash (fix #42).
    With preserve_separators=False: any out-of-charset char passes through
    unchanged (separate behavior, not fixed here)."""
    if not val:
        return val
    charset_set = set(charset)
    if preserve_separators:
        positions = [i for i, ch in enumerate(val) if ch in charset_set]
        if not positions:  # fix #42: covering hash replaces verbatim passthrough
            return _covering_hash_to_charset(val, key, charset, tweak)
        body = _fpe_pure_value(
            "".join(val[i] for i in positions),
            key,
            charset,
            tweak,
            validate_luhn,
            forward=forward,
            checksum=checksum,
        )
        result = list(val)
        for pos, ch in zip(positions, body, strict=False):
            result[pos] = ch
        return "".join(result)
    if not all(ch in charset_set for ch in val):
        return val
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


class FPEStrategy(BaseMaskingStrategy):
    """Format-Preserving Encryption (FPE) mask strategy (Sprint B · Item 32).

    Replaces each value with another value of the same length over the same
    character set.  Same input + same key -> same output (keyed determinism).

    YAML config keys:
      charset: 'digits' | 'alpha' | 'ALPHA' | 'alphanum' | 'ALPHANUM'
               | explicit string   (default: 'digits')
      preserve_separators: bool  (default: true)
        Non-charset characters stay at their original positions; only charset
        characters are encrypted.  Example: "123-45-6789" -> "748-23-1056" with
        charset 'digits' and dashes preserved in-place.
      validate_luhn: bool  (default: false)
        After encryption, replace the last charset character with the Luhn
        check digit computed from the preceding characters.  Useful for
        masking PANs into values that pass card-validation checks.  Silently
        ignored when the charset contains non-digit characters.
      checksum: str  (default: none)
        Scheme name for check-digit recomputation after encryption.
        Supported: 'luhn', 'npi', 'iban', 'vin', 'isbn13', 'ean13', 'gtin'.
        Takes priority over validate_luhn when both are set.
    """

    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        column_name = rule.get("column", "unnamed")
        column_key = self._column_key(column_name)

        # Resolve + validate charset
        charset_spec = rule.get("charset", "digits")
        charset = _CHARSETS.get(charset_spec, charset_spec)
        charset = "".join(dict.fromkeys(charset))  # deduplicate, preserve order
        if len(charset) < 2:
            self.logger.warning(
                f"fpe: charset for '{column_name}' has <2 distinct characters; "
                f"passing column through unchanged"
            )
            return column

        preserve_sep = bool(rule.get("preserve_separators", True))
        validate_luhn = bool(rule.get("validate_luhn", False))
        checksum: str | None = rule.get("checksum") or None
        # Luhn only meaningful over a pure-digit charset (ignored when checksum is set)
        if validate_luhn and checksum is None and not all(c in "0123456789" for c in charset):
            self.logger.warning(
                f"fpe: validate_luhn=true ignored for column '{column_name}' "
                f"because charset contains non-digit characters"
            )
            validate_luhn = False

        # SP-46 (decision C): mirror the V2 join-group tweak resolution so the
        # two paths stay in sync and a V1 caller using fpe_join_group gets the
        # same tweak the V2 path would use. V1 rule dicts are flat; fpe_join_group
        # may appear directly in the rule or nested under provider_config.
        _v1_join_group: str | None = (
            rule.get("fpe_join_group")
            or (rule.get("provider_config") or {}).get("fpe_join_group")
            or None
        )
        tweak = (_v1_join_group or column_name).encode("utf-8", errors="replace")

        if column_key is not None:
            key = column_key
            self.logger.debug(f"Applying keyed FPE to column '{column_name}'")
        else:
            self.logger.debug(f"Applying legacy FPE (no master key) to column '{column_name}'")
            seed_material = f"fpe-legacy-{self.seed}-{column_name}".encode()
            key = hashlib.sha256(seed_material).digest()

        # The encryption itself (8 Feistel rounds, each an HMAC-SHA256) has
        # to run once per value - there's no whole-column equivalent. So
        # this isn't true vectorization; we're just trimming the pandas
        # overhead off the per-row loop. Three things move out of the loop
        # into single whole-column ops: the null check (one C-level mask vs
        # N Python `pd.isna` calls), the string cast (one `.astype(str)`),
        # and the pandas apply machinery itself (a plain list comp is
        # cheaper than `Series.apply`, which boxes/unboxes every scalar).
        # Speedup is small (~3-6x) because the Feistel work dominates total
        # time at any reasonable column size.
        na_mask = column.isna()
        non_na_str = column[~na_mask].astype(str).tolist()
        encrypted = [
            self._encrypt(
                s, key, charset, tweak, preserve_sep, validate_luhn, column_name, checksum
            )
            for s in non_na_str
        ]
        result = column.copy().astype(object)
        result.loc[~na_mask] = encrypted

        self._log_stats(column, result, rule)
        return result

    def _encrypt(
        self,
        val: str,
        key: bytes,
        charset: str,
        tweak: bytes,
        preserve_sep: bool,
        validate_luhn: bool,
        column_name: str,
        checksum: str | None = None,
    ) -> str:
        if not preserve_sep and val and not all(ch in set(charset) for ch in val):
            self.logger.warning(
                f"fpe: value for '{column_name}' contains characters outside "
                f"charset and preserve_separators=false; passing through unchanged"
            )
            return val
        return fpe_encrypt_value(val, key, charset, tweak, preserve_sep, validate_luhn, checksum)

    def _fpe_pure(
        self,
        s: str,
        key: bytes,
        charset: str,
        tweak: bytes,
        validate_luhn: bool,
    ) -> str:
        """FPE a string consisting entirely of charset characters.

        Kept as a thin delegate for established callers; the shared
        forward/inverse implementation lives in `_fpe_pure_value`."""
        return _fpe_pure_value(s, key, charset, tweak, validate_luhn, forward=True)

    def _column_key(self, column_name: str) -> bytes | None:
        """Derive the mask sub-key from the master key resolver (same pattern as HashStrategy).

        QA 2026-05-31 F1 (HIGH) closure: previously a derive_key failure
        silently fell through to the seed-only legacy FPE path, producing
        masked output that was no longer recoverable from the master key
        + not byte-identical to a re-run with the master key. The
        degradation was invisible to the operator (only a WARNING log).
        Now: derive_key failures RAISE so the job fails explicitly + the
        operator gets a typed error in the manifest. derive_key=None
        (legacy seed-only configs that explicitly opted out of the
        master key) still returns None as before; that's an explicit
        opt-out, not a silent degradation.
        """
        if self.derive_key is None:
            return None
        try:
            return self.derive_key("mask")
        except Exception as exc:
            self.logger.error(
                f"FPE: derive_key failed for 'mask' ({type(exc).__name__}: {exc}). "
                "Refusing to silently degrade to seed-only encryption."
            )
            raise MaskKeyDerivationError(
                f"FPE column key derivation failed: {type(exc).__name__}. "
                "Refusing to silently degrade to seed-only encryption; "
                "fix the master key infrastructure + re-run the job.",
                strategy="fpe",
            ) from exc
