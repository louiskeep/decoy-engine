"""The `distribution` generate type: draws from a named parametric family.

One `numpy.random.Generator` per column, seeded from the `"dist"` RNG family of the column's
`GenDeriveContext`. It is not the `"np"` family: the null mask draws
`default_rng(base_int("np")).random(n)` on the same column, and `null_probability` is outside the
fingerprint, so a shared family would give the mask and the value the same uniforms (a uniform
column's nulls would be exactly its lowest values).

NumPy fills an array of n draws element by element from the bit generator, so draws of sizes k
and n - k concatenate to the one draw of n. That makes the stream prefix-stable: the output for
rows `[offset, offset + size)` does not depend on how the rows are split into batches.

Only `uniform` ships. It is built from `random()` (raw PCG64 bits, no libm). The other families
(normal, exponential, lognormal, beta, poisson, zipf) call libm or float pow inside numpy or need
fractional-power normalization, so their last bit is not guaranteed machine-independent; they wait
for an in-repo sampler driven by raw PCG64 bits (plan: Deferred to E1b).

`min` and `max` clip with `numpy.clip` after the draw (one draw per row, so prefix stability
holds). A truncated distribution by resampling would break it and is not offered.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

from decoy_engine.config import parametric_arrow_type
from decoy_engine.generators.derivation import GenDeriveContext

__all__ = [
    "ParametricStream",
    "parametric_arrow_type",
    "parametric_payload_bound",
    "parametric_values",
]

_Draw = Callable[[np.random.Generator, int], "np.ndarray[Any, Any]"]


def _uniform(col: dict[str, Any]) -> _Draw:
    low, high = float(col["low"]), float(col["high"])
    width = high - low
    # Two separate ufuncs, never `rng.uniform(low, high)`: arm64 fuses low + width * x into an FMA
    # and rounds differently from x86. rng.random itself has no libm call and matches on both.
    return lambda rng, size: np.add(np.multiply(rng.random(size), width), low)


# Family -> draw builder. The keys each family takes are `CONDITIONAL_REQUIRED["distribution"]`.
_FAMILIES: dict[str, Callable[[dict[str, Any]], _Draw]] = {
    "uniform": _uniform,
}


def parametric_payload_bound() -> int:
    return 8


class ParametricStream:
    """One persistent Generator per column, advanced `size` draws per call."""

    def __init__(self, col: dict[str, Any], ctx: GenDeriveContext) -> None:
        self._draw = _FAMILIES[str(col.get("family"))](col)
        self._low = col.get("min")
        self._high = col.get("max")
        self._rng = np.random.default_rng(ctx.base_int("dist"))

    def next_batch(self, offset: int, size: int) -> list[Any]:
        draws = self._draw(self._rng, size)
        if self._low is not None or self._high is not None:
            draws = np.clip(draws, self._low, self._high)
        return list(draws.tolist())


def parametric_values(col: dict[str, Any], n: int, ctx: GenDeriveContext) -> list[Any]:
    return ParametricStream(col, ctx).next_batch(0, n)
