"""An independently authored FF1 reference, for differential testing only.

This is deliberately NOT a copy of ``decoy_engine.transforms._ff1``. It is
written straight from the NIST SP 800-38G Algorithm 5/6 text with its own
internal representation (plain integers for the two halves throughout,
rather than numeral lists; ``struct``-based block packing instead of
manual byte concatenation; a single shared round routine parameterised
by direction instead of two near-duplicate loops) so that an agreement
between it and the production module is evidence the two independently
transcribed the same standard, not evidence one was copy-pasted from the
other. It is test-only: nothing under ``src/`` imports this module, and
it must never become a production import.

Not itself locked to the NIST/Wycheproof KATs (the production module is).
Its job is the differential property: for random (key, tweak, radix,
message), oracle output must equal production output, and vice versa on
decrypt.
"""

from __future__ import annotations

import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_NUM_ROUNDS = 10


def _aes_ecb_block(key: bytes, block: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()  # noqa: S305 -- single-block CIPH_K per NIST SP 800-38G; never used to encrypt multi-block data
    return enc.update(block) + enc.finalize()


def _cbc_mac(key: bytes, message: bytes) -> bytes:
    """Textbook CBC-MAC: zero IV, chain forward AES blocks, keep the last."""
    if len(message) % 16:
        raise ValueError("CBC-MAC input must be a multiple of the block size")
    enc = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    out = enc.update(message) + enc.finalize()
    return out[-16:]


def _expand(key: bytes, seed: bytes, out_len: int) -> bytes:
    """Grow ``seed`` (16 bytes) to ``out_len`` bytes via forward-AES blocks
    of ``seed XOR counter``, counter = 1, 2, ... (SP 800-38G S-block)."""
    buf = bytearray(seed)
    counter = 1
    seed_int = int.from_bytes(seed, "big")
    while len(buf) < out_len:
        xored = (seed_int ^ counter).to_bytes(16, "big")
        buf += _aes_ecb_block(key, xored)
        counter += 1
    return bytes(buf[:out_len])


def _fractional_byte_width(radix: int, digit_count: int) -> int:
    """Bytes needed to hold any value in [0, radix**digit_count)."""
    ceiling = radix**digit_count - 1
    if ceiling <= 0:
        return 0
    bit_width = ceiling.bit_length()
    return -(-bit_width // 8)  # ceil-divide without importing math


def _header_block(radix: int, left_len: int, total_len: int, tweak_len: int) -> bytes:
    """Pack the fixed 16-byte header (spec's P) with struct instead of
    manual slicing, as an independent re-derivation of the same 16 bytes."""
    radix_bytes = struct.pack(">I", radix)[1:]  # 3-byte big-endian radix
    return (
        struct.pack(">BBB", 1, 2, 1)
        + radix_bytes
        + struct.pack(">BB", _NUM_ROUNDS, left_len & 0xFF)
        + struct.pack(">II", total_len, tweak_len)
    )


def _round_value(
    key: bytes,
    header: bytes,
    tweak: bytes,
    round_index: int,
    fixed_width: int,
    source_value: int,
) -> int:
    """Compute the round's pseudorandom integer y (spec steps 6.i-6.iv),
    independent code path: builds Q, runs the MAC, grows S, reads y."""
    pad_len = (-len(tweak) - fixed_width - 1) % 16
    q = (
        tweak
        + b"\x00" * pad_len
        + struct.pack(">B", round_index)
        + source_value.to_bytes(fixed_width, "big")
    )
    seed = _cbc_mac(key, header + q)
    block_count = (fixed_width + 3) // 4
    d = 4 * block_count + 4
    s = _expand(key, seed, d)
    return int.from_bytes(s, "big")


def _run(key: bytes, tweak: bytes, radix: int, digits: list[int], *, forward: bool) -> list[int]:
    n = len(digits)
    if n < 2:
        raise ValueError("message must have at least two numerals")
    left_len = n // 2
    right_len = n - left_len
    left_value = 0
    for d in digits[:left_len]:
        left_value = left_value * radix + d
    right_value = 0
    for d in digits[left_len:]:
        right_value = right_value * radix + d

    fixed_width = _fractional_byte_width(radix, right_len)
    header = _header_block(radix, left_len, n, len(tweak))

    order = range(_NUM_ROUNDS) if forward else reversed(range(_NUM_ROUNDS))
    for round_index in order:
        target_len = left_len if round_index % 2 == 0 else right_len
        modulus = radix**target_len
        if forward:
            y = _round_value(key, header, tweak, round_index, fixed_width, right_value)
            combined = (left_value + y) % modulus
            left_value, right_value = right_value, combined
        else:
            y = _round_value(key, header, tweak, round_index, fixed_width, left_value)
            combined = (right_value - y) % modulus
            right_value, left_value = left_value, combined

    out = [0] * n
    remaining = right_value
    for i in range(n - 1, left_len - 1, -1):
        remaining, out[i] = divmod(remaining, radix)
    remaining = left_value
    for i in range(left_len - 1, -1, -1):
        remaining, out[i] = divmod(remaining, radix)
    return out


def oracle_encrypt(key: bytes, tweak: bytes, radix: int, digits: list[int]) -> list[int]:
    return _run(key, tweak, radix, digits, forward=True)


def oracle_decrypt(key: bytes, tweak: bytes, radix: int, digits: list[int]) -> list[int]:
    return _run(key, tweak, radix, digits, forward=False)
