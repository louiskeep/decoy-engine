"""Independent `fractions.Fraction` oracle and seeded corpus for the categorical CDF.

The oracle restates the builder's contract from scratch with rational arithmetic and
shares no code with `decoy_engine` beyond the reason codes and the resolution constant:
threshold_i = floor(C_i * RES / T + 1/2) (round half up) over the exact prefix sum C_i
and total T. Refusals run in whole-vector passes, each reporting its lowest index:
type, non-finite element, negative element, total (above the largest double or zero),
zero-width slot for a positive weight.
"""

from __future__ import annotations

import hashlib
import math
import random
import sys
from fractions import Fraction
from typing import Any

import numpy as np

from decoy_engine import reasons

RES = 1_000_000

NONPOSITIVE = str(reasons.runtime.CATEGORICAL_WEIGHTS_NONPOSITIVE)
NEGATIVE = str(reasons.runtime.CATEGORICAL_WEIGHTS_NEGATIVE)
BELOW_RESOLUTION = str(reasons.runtime.CATEGORICAL_WEIGHT_BELOW_RESOLUTION)

_INTS = {int, np.int8, np.int16, np.int32, np.int64, np.uint8, np.uint16, np.uint32, np.uint64}
_INTS |= {np.longlong, np.ulonglong, np.intc, np.uintc}
_FLOATS = {float, np.float16, np.float32, np.float64}

Outcome = tuple[Any, ...]


def oracle(weights: list[Any]) -> Outcome:
    """`("ok", thresholds)` or `("err", code, index_or_None)`."""
    for i, w in enumerate(weights):
        if type(w) not in _INTS and type(w) not in _FLOATS:
            return ("err", NONPOSITIVE, i)
    exact: list[Fraction] = []
    for i, w in enumerate(weights):
        if type(w) in _FLOATS:
            x = float(w)
            if math.isnan(x) or math.isinf(x):
                return ("err", NONPOSITIVE, i)
            exact.append(Fraction(x))
        else:
            exact.append(Fraction(int(w)))
    for i, f in enumerate(exact):
        if f < 0:
            return ("err", NEGATIVE, i)
    total = sum(exact, Fraction(0))
    if total > Fraction(sys.float_info.max) or total == 0:
        return ("err", NONPOSITIVE, None)
    out: list[int] = []
    running = Fraction(0)
    prev = 0
    for i, f in enumerate(exact):
        running += f
        t = math.floor(running * RES / total + Fraction(1, 2))
        if f > 0 and t == prev:
            return ("err", BELOW_RESOLUTION, i)
        out.append(t)
        prev = t
    return ("ok", tuple(out))


def corpus(n: int = 20_000, seed: int = 20261031) -> list[list[float]]:
    """Seeded weight vectors: 2 to 200 categories in four families (rounded decimals,
    raw `random()`, normalized to 1.0, repeated values with zeros). Uses only
    `Random.random` and `Random.randint`, whose streams are fixed across 3.10 to 3.12."""
    rng = random.Random(seed)
    out: list[list[float]] = []
    for k in range(n):
        size = rng.randint(2, 200)
        family = k % 4
        if family == 0:
            vec = [round(rng.random(), rng.randint(1, 4)) + 0.001 for _ in range(size)]
        elif family == 1:
            vec = [rng.random() + 1e-3 for _ in range(size)]
        elif family == 2:
            raw = [rng.random() + 1e-3 for _ in range(size)]
            s = 0.0
            for x in raw:
                s += x
            vec = [x / s for x in raw]
        else:
            pool = [0.0, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5]
            vec = [pool[rng.randint(0, len(pool) - 1)] for _ in range(size)]
            vec[rng.randint(0, size - 1)] = 1.0
        out.append(vec)
    return out


def error_corpus(n: int = 3_000, seed: int = 7) -> list[list[Any]]:
    """Vectors with injected refusals (two per vector so pass order matters)."""
    rng = random.Random(seed)
    bad: list[Any] = [
        -0.5,
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        "x",
        None,
        1e-9,
        0.0,
        np.float32("nan"),
        np.bool_(True),
    ]
    out: list[list[Any]] = []
    for _ in range(n):
        size = rng.randint(2, 12)
        vec: list[Any] = [rng.random() for _ in range(size)]
        for _ in range(2):
            vec[rng.randint(0, size - 1)] = bad[rng.randint(0, len(bad) - 1)]
        out.append(vec)
    return out


def outcome_digest(outcomes: list[Outcome]) -> str:
    h = hashlib.sha256()
    for o in outcomes:
        h.update(repr(o).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()
