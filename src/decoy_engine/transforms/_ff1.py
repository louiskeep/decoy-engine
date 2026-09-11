"""NIST SP 800-38G FF1 format-preserving encryption (Algorithms 5 and 6).

Normative to NIST Special Publication 800-38G, "Recommendation for Block
Cipher Modes of Operation: Methods for Format-Preserving Encryption"
(final publication, plus the pinned restrictions from the "Rev. 1 SECOND
PUBLIC DRAFT, 2025-02-03": FF1 is the sole surviving method, since FF3/
FF3-1 were withdrawn after the Beyne 2021 attack, and the minimum-domain
floor is raised; see ``FF1_MIN_DOMAIN`` below). This module implements
only the raw Algorithm 5 (``encrypt``) / Algorithm 6 (``decrypt``)
primitive over numeral strings. The deployable-profile constants at the
bottom of the module (radix bounds, length bounds, the domain floor) are
the pinned parameters the caller (``transforms/fpe.py``) enforces before
calling in; this module does not itself refuse a sub-floor domain, since
that is a deployment-policy decision made one layer up, not a property
of the Feistel-network construction. NIST's own published KAT corpus
(source-noted in the test suite) exercises both AES-128/192/256 and
sub-floor message lengths, confirming the raw algorithm is faithful
across its full mathematical domain.

Source pattern: a from-the-standard implementation, per the repo rule to
survey and cite the established approach rather than inventing one.
Every non-cryptographic operation is exact-arithmetic on Python's
arbitrary-precision ints; there is no float anywhere in this module, by
requirement (a rounding error in ciphertext selection would corrupt
output silently). The single cryptographic primitive is AES, used
forward-only (no inverse-cipher option; Rev.1 2PD explicitly prohibits
it) via the audited `cryptography` package. This module never implements
or touches AES internals itself.

CBC-MAC PRF (Algorithm 5/6 step 6.ii): implemented as AES-CBC encryption
under an all-zero IV over the block-aligned ``P || Q``, keeping only the
final ciphertext block. That is the standard CBC-MAC-from-CBC-encrypt
equivalence: CBC-MAC's definition is "encrypt every block chained from a
zero IV, keep the last one," and the intermediate ciphertext blocks are
discarded scratch. This lets the audited library's chaining mode do the
chaining instead of hand-rolling a block loop.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ---------------------------------------------------------------------------
# Deployable-profile constants (NIST SP 800-38G Rev.1 2PD pinned parameters).
#
# These are NOT enforced inside encrypt()/decrypt() below: the primitive
# is deliberately general so it can be locked against the full published
# KAT corpus (which includes AES-128/192/256 and sub-floor message
# lengths). The caller (the `fpe` strategy handler, Stage 2 of this task)
# is responsible for enforcing this profile before ever calling encrypt/
# decrypt, in the pinned validation order recorded there.
# ---------------------------------------------------------------------------

FF1_MIN_RADIX = 2
FF1_MAX_RADIX = 64
FF1_MAX_LEN = 256  # numerals; practical cap, far under the spec's 2**32 ceiling
FF1_MAX_TWEAK_LEN = 256  # bytes
FF1_KEY_BYTES = 32  # AES-256 only in the deployed profile (P2: "AES key length: 256 only")

# SP 800-38G Rev.1 2PD raises the minimum domain; pin to the revision, not a
# bare literal, so a future spec change is a one-line, reviewed diff.
FF1_MIN_DOMAIN = 1_000_000

_ROUNDS = 10  # SP 800-38G Algorithm 5/6 step 6: "for i from 0 to 9"


def min_domain_length(radix: int) -> int:
    """Smallest numeral-string length L such that ``radix**L >= FF1_MIN_DOMAIN``.

    Pure integer search (no ``math.log``, so no float-precision risk at the
    boundary): a caller enforcing the domain floor needs the EXACT L, not an
    approximation that could be off by one at the threshold.
    """
    if radix < 2:
        raise ValueError(f"radix must be >= 2; got {radix}")
    length = 1
    value = radix
    while value < FF1_MIN_DOMAIN:
        length += 1
        value *= radix
    return length


class Ff1Error(ValueError):
    """Invalid input to the FF1 primitive (parameter, not domain policy).

    Deliberately a plain, local exception: this module has no dependency
    on the engine's error taxonomy (``decoy_engine.errors``); the caller in
    ``transforms/fpe.py`` translates these into the engine's typed,
    redacted errors (``FpeUnencryptableError`` etc.) per the Stage 2 wiring.
    """


def _ciph(key: bytes, block: bytes) -> bytes:
    """Single forward AES block encryption (ECB of exactly one 16-byte block).

    This is ``CIPH_K`` in the spec's notation: forward cipher only, per the
    Rev.1 2PD prohibition on the inverse-cipher optimization.
    """
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()  # noqa: S305 -- single-block CIPH_K per NIST SP 800-38G; never used to encrypt multi-block data, so ECB's pattern-leak weakness does not apply
    return encryptor.update(block) + encryptor.finalize()


def _prf(key: bytes, data: bytes) -> bytes:
    """CBC-MAC over ``data`` (must be a multiple of 16 bytes): the last block
    of AES-CBC-encrypting ``data`` under an all-zero IV, forward-AES-only."""
    if len(data) % 16 != 0:
        raise Ff1Error(f"PRF input must be block-aligned; got {len(data)} bytes")
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    ciphertext = encryptor.update(data) + encryptor.finalize()
    return ciphertext[-16:]


def _s_block(key: bytes, r: bytes, d: int) -> bytes:
    """Expand the 16-byte PRF output ``R`` to ``d`` bytes (spec step 6.iii).

    d <= 16 needs no expansion (S = R, truncated to d bytes: a no-op
    truncation when d == 16). d > 16 appends forward-AES blocks of
    ``R XOR j`` (j = 1, 2, ... as a 16-byte big-endian integer) until there
    are enough bytes, per SP 800-38G's S-block construction.
    """
    s = bytearray(r)
    j = 1
    while len(s) < d:
        block = int.from_bytes(r, "big") ^ j
        s.extend(_ciph(key, block.to_bytes(16, "big")))
        j += 1
    return bytes(s[:d])


def _num_radix(numerals: list[int], radix: int) -> int:
    """NUM_radix(X): the big-integer value of a numeral list, MSB first."""
    x = 0
    for numeral in numerals:
        x = x * radix + numeral
    return x


def _str_m_radix(x: int, m: int, radix: int) -> list[int]:
    """STR_m_radix(x): fixed-width, MSB-first numeral list of length m.

    Exact-arithmetic digit extraction (repeated divmod); the final ``x``
    must reach exactly 0, or ``x`` did not fit in ``m`` base-``radix``
    digits; that is a caller bug, not a value to silently truncate or wrap.
    """
    digits = [0] * m
    for i in range(m - 1, -1, -1):
        x, digit = divmod(x, radix)
        digits[i] = digit
    if x != 0:
        raise Ff1Error(f"value does not fit in {m} base-{radix} digits (overflow)")
    return digits


def _byte_len_for_radix_power(radix: int, length: int) -> int:
    """b = ceil(ceil(length * LOG2(radix)) / 8), computed as exact bit-length
    arithmetic (no ``math.log2``): ``ceil(length * log2(radix))`` is exactly
    the number of bits needed to hold any value in ``[0, radix**length)``,
    i.e. ``(radix**length - 1).bit_length()`` when that value is positive.
    """
    max_value = radix**length - 1
    bits = max_value.bit_length() if max_value > 0 else 0
    return (bits + 7) // 8


def _build_p(radix: int, u: int, n: int, tweak_len: int) -> bytes:
    """The fixed 16-byte P block (spec step 5): version/method/rounds
    constants, radix, half-length parity byte, total length, tweak length."""
    return (
        bytes([1, 2, 1])
        + radix.to_bytes(3, "big")
        + bytes([_ROUNDS, u & 0xFF])
        + n.to_bytes(4, "big")
        + tweak_len.to_bytes(4, "big")
    )


def _validate_common(key: bytes, tweak: bytes, radix: int, numerals: list[int]) -> tuple[int, int]:
    if len(key) not in (16, 24, 32):
        raise Ff1Error(f"AES key must be 16, 24, or 32 bytes; got {len(key)}")
    if radix < 2:
        raise Ff1Error(f"radix must be >= 2; got {radix}")
    n = len(numerals)
    if n < 2:
        raise Ff1Error(f"numeral string must have length >= 2 (NIST FF1 precondition); got {n}")
    if any(not (0 <= digit < radix) for digit in numerals):
        raise Ff1Error(f"every numeral must be in [0, {radix}); got {numerals!r}")
    if len(tweak) >= 2**32:
        raise Ff1Error(f"tweak length must fit in a uint32; got {len(tweak)} bytes")
    return n, len(tweak)


def encrypt(key: bytes, tweak: bytes, radix: int, numerals: list[int]) -> list[int]:
    """FF1.Encrypt(K, T, X): NIST SP 800-38G Algorithm 5.

    ``numerals`` is X: a list of integers in ``[0, radix)``, MSB first.
    Returns the ciphertext numeral list, same length as ``numerals``.
    """
    n, t = _validate_common(key, tweak, radix, numerals)
    u = n // 2
    v = n - u
    a = list(numerals[:u])
    b = list(numerals[u:])
    byte_len = _byte_len_for_radix_power(radix, v)
    d = 4 * ((byte_len + 3) // 4) + 4
    p = _build_p(radix, u, n, t)
    zero_pad = bytes((-t - byte_len - 1) % 16)

    for i in range(_ROUNDS):
        q = tweak + zero_pad + bytes([i]) + _num_radix(b, radix).to_bytes(byte_len, "big")
        r = _prf(key, p + q)
        s = _s_block(key, r, d)
        y = int.from_bytes(s, "big")
        m = u if i % 2 == 0 else v
        c = (_num_radix(a, radix) + y) % (radix**m)
        c_numerals = _str_m_radix(c, m, radix)
        a, b = b, c_numerals

    return a + b


def decrypt(key: bytes, tweak: bytes, radix: int, numerals: list[int]) -> list[int]:
    """FF1.Decrypt(K, T, X): NIST SP 800-38G Algorithm 6.

    Inverse of ``encrypt`` under the same ``(key, tweak, radix)``: a
    bijection over the numeral-string domain, run subtractive/reversed
    (rounds applied 9 downto 0, subtracting the round value instead of
    adding it) per the spec's decrypt direction.
    """
    n, t = _validate_common(key, tweak, radix, numerals)
    u = n // 2
    v = n - u
    a = list(numerals[:u])
    b = list(numerals[u:])
    byte_len = _byte_len_for_radix_power(radix, v)
    d = 4 * ((byte_len + 3) // 4) + 4
    p = _build_p(radix, u, n, t)
    zero_pad = bytes((-t - byte_len - 1) % 16)

    for i in reversed(range(_ROUNDS)):
        q = tweak + zero_pad + bytes([i]) + _num_radix(a, radix).to_bytes(byte_len, "big")
        r = _prf(key, p + q)
        s = _s_block(key, r, d)
        y = int.from_bytes(s, "big")
        m = u if i % 2 == 0 else v
        c = (_num_radix(b, radix) - y) % (radix**m)
        c_numerals = _str_m_radix(c, m, radix)
        b, a = a, c_numerals

    return a + b
