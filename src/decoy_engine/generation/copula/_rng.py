"""Randomness framing for the copula: purpose labels, the seed check, and uniforms from raw output.

The stream itself is `PCG64(SeedSequence(int(sha256(framed(label) || len(seed) || seed))))`,
written out in `fit_gaussian_copula` and `sample_gaussian_copula` themselves so the seed is bound
only in those two wiped frames (`tests/unit/generation/copula/test_rng_guard.py` checks the two
derivations are the same expression and that no other code builds a generator).

Pattern follows NumPy NEP-19 (`PCG64` bit-stream stability, O'Neill 2014) with `advance()` for a
seekable stream, the counter-based idea `generation/window.py` cites (Salmon et al., SC 2011).
Raw 64-bit outputs are converted to doubles here rather than through `Generator` methods, so every
uniform consumes exactly one output (`standard_normal`'s ziggurat consumes a variable number,
which would break seeking) and the stream does not depend on `Generator` internals.
"""

from __future__ import annotations

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


def check_seed(seed: object) -> None:
    try:
        if not isinstance(seed, bytes):
            raise TypeError("copula: seed must be bytes.")
        if len(seed) < MIN_SEED_BYTES:
            raise ValueError(f"copula: seed must be at least {MIN_SEED_BYTES} bytes.")
    except BaseException:
        del seed
        raise


def framed(label: bytes) -> bytes:
    """The length-prefixed purpose label that starts the seed derivation."""
    return len(label).to_bytes(4, "big") + label


def raw_to_uniform(raw: npt.NDArray[np.uint64]) -> npt.NDArray[np.float64]:
    return ((raw >> np.uint64(_SHIFT)).astype(np.float64) + 0.5) * _SCALE


def uniforms(bitgen: BitGenerator, count: int) -> npt.NDArray[np.float64]:
    """`count` uniforms in (0, 1), consuming exactly `count` raw outputs."""
    raw = np.asarray(bitgen.random_raw(count), dtype=np.uint64)
    return raw_to_uniform(raw)
