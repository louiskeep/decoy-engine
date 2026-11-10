"""The `distribution` generate type: draws from a named parametric family.

One `numpy.random.Generator` per column, seeded from the `"dist"` RNG family of the column's
`GenDeriveContext`. It is not the `"np"` family: the null mask draws
`default_rng(base_int("np")).random(n)` on the same column, and `null_probability` is outside the
fingerprint, so a shared family would give the mask and the value the same uniforms (a uniform
column's nulls would be exactly its lowest values).

NumPy fills an array of n draws element by element from the bit generator, so draws of sizes k
and n - k concatenate to the one draw of n. That makes the stream prefix-stable: the output for
rows `[offset, offset + size)` does not depend on how the rows are split into batches. The bounded
Zipf draws one `random()` per row through the same Generator and maps it by inverse CDF.

`min` and `max` clip with `numpy.clip` after the draw (one draw per row, so prefix stability
holds). A truncated distribution by resampling would break it and is not offered.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pyarrow as pa

from decoy_engine.config._generate_type_checks import INT_FAMILIES
from decoy_engine.generators.derivation import GenDeriveContext

_Draw = Callable[[np.random.Generator, int], "np.ndarray[Any, Any]"]


def zipf_cdf(s: float, n: int) -> np.ndarray[Any, Any]:
    """Cumulative probability of ranks 1..n under P(k) proportional to 1 / k**s (finite Zipf)."""
    weights = np.arange(1, n + 1, dtype=np.float64) ** -s
    cumulative = np.cumsum(weights)
    return np.asarray(cumulative / cumulative[-1])


def _normal(col: dict[str, Any]) -> _Draw:
    mean, std = float(col["mean"]), float(col["std"])
    return lambda rng, size: rng.normal(mean, std, size)


def _lognormal(col: dict[str, Any]) -> _Draw:
    mu, sigma = float(col["mu"]), float(col["sigma"])
    return lambda rng, size: rng.lognormal(mu, sigma, size)


def _uniform(col: dict[str, Any]) -> _Draw:
    low, high = float(col["low"]), float(col["high"])
    return lambda rng, size: rng.uniform(low, high, size)


def _exponential(col: dict[str, Any]) -> _Draw:
    mean = float(col["mean"])
    return lambda rng, size: rng.exponential(mean, size)


def _poisson(col: dict[str, Any]) -> _Draw:
    mean = float(col["mean"])
    return lambda rng, size: rng.poisson(mean, size)


def _beta(col: dict[str, Any]) -> _Draw:
    alpha, beta = float(col["alpha"]), float(col["beta"])
    return lambda rng, size: rng.beta(alpha, beta, size)


def _zipf(col: dict[str, Any]) -> _Draw:
    support = int(col["n"])
    cdf = zipf_cdf(float(col["s"]), support)  # built once per stream, not per batch

    def draw(rng: np.random.Generator, size: int) -> np.ndarray[Any, Any]:
        ranks = np.searchsorted(cdf, rng.random(size), side="left")
        return np.minimum(ranks, support - 1) + 1

    return draw


# Family -> draw builder. The keys each family takes are `CONDITIONAL_REQUIRED["distribution"]`.
_FAMILIES: dict[str, Callable[[dict[str, Any]], _Draw]] = {
    "normal": _normal,
    "lognormal": _lognormal,
    "uniform": _uniform,
    "exponential": _exponential,
    "poisson": _poisson,
    "beta": _beta,
    "zipf": _zipf,
}


def parametric_arrow_type(col: dict[str, Any]) -> pa.DataType:
    return pa.int64() if col.get("family") in INT_FAMILIES else pa.float64()


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
