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


def _fpe_pure_value(
    s: str, key: bytes, charset: str, tweak: bytes, validate_luhn: bool, *, forward: bool
) -> str:
    """FPE (or invert) a string consisting entirely of charset characters.

    Luhn mode permutes the BODY (all chars but the last) and appends the
    Luhn check digit of the result, in both directions. Encrypt output is
    Luhn-valid by construction; decrypt restores the body exactly and
    recomputes the check digit, so a Luhn-valid source (the domain the
    mode exists for: PANs) round-trips byte-exactly. The pre-WS1 shape
    (permute all n chars, overwrite the last with the check digit)
    discarded one encrypted character and was therefore not invertible;
    the change is covered by the SEED_PROTOCOL_VERSION 4 -> 5 bump."""
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
    *,
    forward: bool,
) -> str:
    """Shared encrypt/decrypt orchestration over one value.

    Separator handling is symmetric: charset characters are extracted,
    permuted as one string, and written back to their original positions,
    so decrypt sees exactly the layout encrypt produced. Values with no
    charset characters (or, with preserve_separators=false, any
    out-of-charset character) pass through unchanged in both directions."""
    if not val:
        return val
    charset_set = set(charset)
    if preserve_separators:
        positions = [i for i, ch in enumerate(val) if ch in charset_set]
        if not positions:
            return val
        body = _fpe_pure_value(
            "".join(val[i] for i in positions), key, charset, tweak, validate_luhn,
            forward=forward,
        )
        result = list(val)
        for pos, ch in zip(positions, body, strict=False):
            result[pos] = ch
        return "".join(result)
    if not all(ch in charset_set for ch in val):
        return val
    return _fpe_pure_value(val, key, charset, tweak, validate_luhn, forward=forward)


def fpe_encrypt_value(
    val: str,
    key: bytes,
    charset: str,
    tweak: bytes,
    preserve_separators: bool = True,
    validate_luhn: bool = False,
) -> str:
    """Encrypt one value with the keyed format-preserving permutation."""
    return _fpe_value(
        val, key, charset, tweak, preserve_separators, validate_luhn, forward=True
    )


def fpe_decrypt_value(
    val: str,
    key: bytes,
    charset: str,
    tweak: bytes,
    preserve_separators: bool = True,
    validate_luhn: bool = False,
) -> str:
    """Invert `fpe_encrypt_value` under the same (key, charset, tweak, config).

    With validate_luhn=true the trailing check digit is recomputed rather
    than stored, so the round-trip is exact iff the source satisfied Luhn
    (see `_fpe_pure_value`)."""
    return _fpe_value(
        val, key, charset, tweak, preserve_separators, validate_luhn, forward=False
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
        # Luhn only meaningful over a pure-digit charset
        if validate_luhn and not all(c in "0123456789" for c in charset):
            self.logger.warning(
                f"fpe: validate_luhn=true ignored for column '{column_name}' "
                f"because charset contains non-digit characters"
            )
            validate_luhn = False

        tweak = column_name.encode("utf-8", errors="replace")

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
            self._encrypt(s, key, charset, tweak, preserve_sep, validate_luhn, column_name)
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
    ) -> str:
        if not preserve_sep and val and not all(ch in set(charset) for ch in val):
            self.logger.warning(
                f"fpe: value for '{column_name}' contains characters outside "
                f"charset and preserve_separators=false; passing through unchanged"
            )
            return val
        return fpe_encrypt_value(val, key, charset, tweak, preserve_sep, validate_luhn)

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
            raise RuntimeError(
                f"FPE column key derivation failed: {type(exc).__name__}. "
                "Refusing to silently degrade to seed-only encryption; "
                "fix the master key infrastructure + re-run the job."
            ) from exc
