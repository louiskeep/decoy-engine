"""Crypto-PAn: keyed prefix-preserving permutation of IP addresses, forward and inverse.

Pattern: Crypto-PAn prefix-preserving IP address anonymization (Xu, Fan, Ammar and Moon, ICNP 2002; cryptography, Apache-2.0/BSD). See: https://doi.org/10.1109/ICNP.2002.1181415.

Output bit ``i`` is input bit ``i`` XOR the top bit of ``AES_K(prefix_i || pad)``, where
``prefix_i`` is the input's first ``i`` bits and ``pad`` (``AES_K(K2)``) fills the rest of the
128-bit block. The 32-byte key is ``K || K2`` (AES-128 key, pad seed), the reference
implementation's layout. Any two inputs sharing a k-bit prefix map to outputs sharing exactly a
k-bit prefix (Theorem 1 of the paper). IPv6 runs the same rule over 128 bits, and IPv4 sits in
the top 32 bits of the block, as in yacryptopan, so one primitive serves both widths. ``keep``
forces the first ``keep`` flips to zero: the output is the input's first ``keep`` bits followed
by the plain Crypto-PAn output's remaining bits.

The inverse is the published bit-by-bit one: each recovered input bit extends the prefix the
next flip is computed from. Crypto-PAn is unauthenticated, so a wrong key inverts to plausible
wrong addresses.

ECB here is the raw block-cipher PRF the construction specifies, one independent block per bit,
not a confidentiality mode; batching all of an address's blocks into one ``update`` call keeps
the per-address cost to one C call. Locked to the reference implementation's 100-address sample
trace and yacryptopan's published examples (``tests/unit/transforms/ip_prefix``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from decoy_engine._opaque import OpaqueBox

# 0xFF never occurs in UTF-8 and a canonical integer source starts with 0x00, so no cell a keyed
# strategy canonicalises (generation/pool/_canonicalize.py) can equal this label and turn a hash
# of that cell into this key.
CRYPTOPAN_KEY_LABEL: Final[bytes] = b"\xffdecoy-cryptopan-key/v1"

_BLOCK_BITS: Final[int] = 128
_ALL_ONES: Final[int] = (1 << _BLOCK_BITS) - 1


class CryptoPan:
    """One Crypto-PAn key. Holds the key only inside the AES context and the pad as an int."""

    __slots__ = ("_aes", "_pad")

    def __init__(self, box: OpaqueBox) -> None:
        # The box holds the 32 derived bytes; unpacked only inside these expressions, so no
        # frame local ever names them. ECB is the single-block AES PRF Crypto-PAn specifies
        # (each block is one bit's independent input), not a mode encrypting a message.
        mode = modes.ECB()  # noqa: S305
        encryptor = Cipher(algorithms.AES(box.open()[0][:16]), mode).encryptor()
        self._aes: Callable[[bytes], bytes] = encryptor.update
        self._pad: int = int.from_bytes(self._aes(box.open()[0][16:32]), "big")

    def __repr__(self) -> str:
        return "<CryptoPan>"

    def _block(self, left_aligned: int, position: int) -> bytes:
        prefix = (left_aligned >> (_BLOCK_BITS - position)) << (_BLOCK_BITS - position)
        return (prefix | (self._pad & (_ALL_ONES >> position))).to_bytes(16, "big")

    def encrypt(self, address: int, width: int, keep: int) -> int:
        """Map a ``width``-bit address, leaving its first ``keep`` bits unchanged."""
        left_aligned = address << (_BLOCK_BITS - width)
        blocks = b"".join(self._block(left_aligned, pos) for pos in range(keep, width))
        out = self._aes(blocks)
        flips = 0
        for i in range(width - keep):
            flips = (flips << 1) | (out[16 * i] >> 7)
        return address ^ flips

    def decrypt(self, output: int, width: int, keep: int) -> int:
        """Invert ``encrypt`` under the same key, width and keep."""
        address = (output >> (width - keep)) << (width - keep) if keep else 0
        for pos in range(keep, width):
            flip = self._aes(self._block(address << (_BLOCK_BITS - width), pos))[0] >> 7
            shift = width - 1 - pos
            address |= (((output >> shift) & 1) ^ flip) << shift
        return address
