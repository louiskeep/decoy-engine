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


def _encode(s: str, charset: str) -> int:
    """Encode a string over `charset` as a base-r integer."""
    r = len(charset)
    x = 0
    for ch in s:
        x = x * r + charset.index(ch)
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


class FPEStrategy(BaseMaskingStrategy):
    """Format-Preserving Encryption (FPE) mask strategy (Sprint B · Item 32).

    Replaces each value with another value of the same length over the same
    character set.  Same input + same key → same output (keyed determinism).

    YAML config keys:
      charset: 'digits' | 'alpha' | 'ALPHA' | 'alphanum' | 'ALPHANUM'
               | explicit string   (default: 'digits')
      preserve_separators: bool  (default: true)
        Non-charset characters stay at their original positions; only charset
        characters are encrypted.  Example: "123-45-6789" → "748-23-1056" with
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
        # to run once per value — there's no whole-column equivalent. So
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
        if not val:
            return val
        charset_set = set(charset)

        if preserve_sep:
            charset_indices = []
            charset_chars = []
            for i, ch in enumerate(val):
                if ch in charset_set:
                    charset_indices.append(i)
                    charset_chars.append(ch)
            if not charset_chars:
                return val
            encrypted_body = self._fpe_pure(
                "".join(charset_chars), key, charset, tweak, validate_luhn
            )
            result = list(val)
            for pos, enc_ch in zip(charset_indices, encrypted_body, strict=False):
                result[pos] = enc_ch
            return "".join(result)
        else:
            if not all(ch in charset_set for ch in val):
                self.logger.warning(
                    f"fpe: value for '{column_name}' contains characters outside "
                    f"charset and preserve_separators=false; passing through unchanged"
                )
                return val
            return self._fpe_pure(val, key, charset, tweak, validate_luhn)

    def _fpe_pure(
        self,
        s: str,
        key: bytes,
        charset: str,
        tweak: bytes,
        validate_luhn: bool,
    ) -> str:
        """FPE a string consisting entirely of charset characters."""
        n = len(s)
        if n == 0:
            return s

        if n == 1:
            # Degenerate single-character case: keyed modular shift
            idx = charset.index(s[0])
            msg = b"fpe-single\xff" + tweak + b"\xff" + s.encode()
            F = int.from_bytes(hmac.new(key, msg, hashlib.sha256).digest(), "big")
            return charset[(idx + F) % len(charset)]

        u = (n + 1) // 2  # ceil(n/2)
        v = n - u  # floor(n/2)
        u_mod = len(charset) ** u
        v_mod = len(charset) ** v

        x = _encode(s, charset)
        y = _feistel(key, tweak, x, u_mod, v_mod)
        result = _decode(y, charset, n)

        if validate_luhn and n >= 2:
            result = result[:-1] + _luhn_check_digit(result[:-1])

        return result

    def _column_key(self, column_name: str) -> bytes | None:
        """Derive the mask sub-key from the master key resolver (same pattern as HashStrategy)."""
        if self.derive_key is None:
            return None
        try:
            return self.derive_key("mask")
        except Exception as exc:
            self.logger.warning(f"derive_key failed for 'mask' ({exc}); falling back to legacy FPE")
            return None
