"""The copula's only source of randomness: a PCG64 stream keyed by caller-supplied seed bytes.

Pattern follows NumPy NEP-19 (`PCG64` bit-stream stability, O'Neill 2014) with `advance()` for a
seekable stream, the counter-based idea `generation/window.py` cites (Salmon et al., SC 2011).
Raw 64-bit outputs are converted to doubles here rather than through `Generator` methods, so every
uniform consumes exactly one output (`standard_normal`'s ziggurat consumes a variable number,
which would break seeking) and the stream does not depend on `Generator` internals.
"""

from __future__ import annotations

import hashlib

import numpy as np
import numpy.typing as npt

MIN_SEED_BYTES = 16
FIT_LABEL = b"decoy-gaussian-copula/v1/fit"
SAMPLE_LABEL = b"decoy-gaussian-copula/v1/sample"

# The top 52 bits plus a half step: every result is an exact double in [2**-53, 1 - 2**-53], so a
# uniform is never 0 or 1 and the inverse normal CDF never sees an endpoint.
BitGenerator = np.random.PCG64

_SHIFT = 12
_SCALE = 2.0**-52


def check_seed(seed: object) -> bytes:
    if not isinstance(seed, bytes):
        raise TypeError("copula: seed must be bytes.")
    if len(seed) < MIN_SEED_BYTES:
        raise ValueError(f"copula: seed must be at least {MIN_SEED_BYTES} bytes.")
    return seed


def stream(seed: bytes, label: bytes) -> BitGenerator:
    """A PCG64 stream for one purpose; the label separates fit from sample for the same bytes."""
    material = len(label).to_bytes(4, "big") + label + len(seed).to_bytes(4, "big") + seed
    entropy = int.from_bytes(hashlib.sha256(material).digest(), "big")
    return np.random.PCG64(np.random.SeedSequence(entropy))


def raw_to_uniform(raw: npt.NDArray[np.uint64]) -> npt.NDArray[np.float64]:
    return ((raw >> np.uint64(_SHIFT)).astype(np.float64) + 0.5) * _SCALE


def uniforms(bitgen: BitGenerator, count: int) -> npt.NDArray[np.float64]:
    """`count` uniforms in (0, 1), consuming exactly `count` raw outputs."""
    raw = np.asarray(bitgen.random_raw(count), dtype=np.uint64)
    return raw_to_uniform(raw)
