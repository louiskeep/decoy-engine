"""Namespace-independence Hamming-distance test (L2 resolution).

For two distinct namespace strings and a fixed seed, the XOR of
derive(seed, N_a, src) and derive(seed, N_b, src) over many source
values should have an average Hamming distance near 128 bits (half of
the 256-bit output), with standard deviation approximately
`sqrt(256 * 0.5 * 0.5) ~ 8 bits`.

The assertion window plus or minus 16 bits is ~2 sigma; if CI proves
flaky at this threshold, the fallback documented in the S3 spec is
plus or minus 24 bits (~3 sigma).
"""

from __future__ import annotations

import pytest

from decoy_engine.determinism import derive

_SEED = b"\x00" * 8
_NS_A = "ns-a"
_NS_B = "ns-b"
_SAMPLES = 10_000
_EXPECTED_MEAN = 128  # bits, for 256-bit output uniform under HMAC-SHA256
_WINDOW = 16  # plus or minus, ~2 sigma


def _hamming_distance(a: bytes, b: bytes) -> int:
    """Count differing bits between two equal-length byte strings."""
    return sum(bin(x ^ y).count("1") for x, y in zip(a, b, strict=True))


@pytest.mark.golden
class TestNamespaceIndependence:
    def test_xor_hamming_distance_is_near_128_bits(self) -> None:
        """Across 10_000 sources, the average Hamming distance of
        derive(seed, ns_a, src) XOR derive(seed, ns_b, src) is within
        [112, 144] bits (128 plus or minus 16).

        Assumption: HMAC-SHA256 output is computationally indistinguishable
        from uniform random under the standard security assumption; the
        expected Hamming distance between two uniform 256-bit values is
        128 bits with stdev ~8 bits. The 16-bit window is ~2 sigma.
        """
        total = 0
        for i in range(_SAMPLES):
            src = str(i).encode()
            a = derive(_SEED, _NS_A, src)
            b = derive(_SEED, _NS_B, src)
            total += _hamming_distance(a, b)
        avg = total / _SAMPLES
        assert _EXPECTED_MEAN - _WINDOW <= avg <= _EXPECTED_MEAN + _WINDOW, (
            f"namespace-independence Hamming-distance off: avg={avg:.2f} "
            f"expected in [{_EXPECTED_MEAN - _WINDOW}, {_EXPECTED_MEAN + _WINDOW}]. "
            "If CI proves flaky at this threshold, S3 spec L2 documents the "
            "plus-or-minus-24-bit (~3 sigma) fallback."
        )
