"""One column's marginal: built from its snapshot entry, inverted at sampling, encoded at fit.

Numeric columns use the snapshot's equal-width histogram with the inverse CDF and a uniform
position inside the chosen bin (Devroye, Non-Uniform Random Variate Generation, 1986, ch. II.2),
the method `generation/statistical/_sample.py` uses, vectorized and driven by a correlated uniform.

Categorical columns and numeric null indicators use the frequency-interval encoding of SDV's
GaussianCopula (RDT `UniformEncoder`, method only; Patki et al., IEEE DSAA 2016): each state owns a
sub-interval of [0, 1) whose width is its frequency; null is one more state. States are ordered by
(count descending, kind, label) so the layout depends only on counts and labels; the snapshot's
collapsed tail (`other`) is always last and is never emitted: at sampling, `u` is scaled by
`1 - p_other` so its mass is redistributed over the kept states in proportion, as `statistical`'s
default `other_mode: redistribute` does.

Lookups accept the closed interval [0, 1]: `ndtr` returns exactly 0 or 1 in the far tails, and
`u * total` can round up to `total`, so the index is clamped to the first and last positive-weight
bin or state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from decoy_engine.generation.copula._model import (
    KIND_CATEGORICAL,
    KIND_EMPTY,
    KIND_NUMERIC,
    ROLE_NULL,
    ColumnMarginal,
)

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

STATE_LABEL = "label"
STATE_NULL = "null"
STATE_PRESENT = "present"
STATE_OTHER = "other"
_KIND_RANK = {STATE_LABEL: 0, STATE_PRESENT: 0, STATE_NULL: 1}
_UNIT_LOW = 2.0**-53
_UNIT_HIGH = 1.0 - 2.0**-53


def from_snapshot_entry(name: str, entry: Mapping[str, Any]) -> ColumnMarginal:
    """The one builder of a column marginal: copies the snapshot entry's fields unchanged."""
    kind = entry.get("kind")
    stats = entry.get("stats") or {}
    common = {
        "name": name,
        "kind": kind,
        "dtype": entry.get("dtype"),
        "null_count": entry.get("null_count"),
        "non_null_count": entry.get("non_null_count"),
    }
    if kind == KIND_NUMERIC:
        return ColumnMarginal(
            **common,  # type: ignore[arg-type]
            bin_edges=tuple(stats.get("bin_edges") or ()),
            bin_counts=tuple(stats.get("bin_counts") or ()),
        )
    if kind == KIND_CATEGORICAL:
        top = tuple(
            (item.get("value"), item.get("count")) for item in stats.get("top_values") or ()
        )
        return ColumnMarginal(
            **common,  # type: ignore[arg-type]
            top_values=top,
            other_count=stats.get("other_count", 0),
        )
    if kind == KIND_EMPTY:
        return ColumnMarginal(**common)  # type: ignore[arg-type]
    raise ValueError(
        f"copula: column {name!r} has snapshot kind {kind!r}; only numeric and categorical "
        "columns (and all-null columns) are supported."
    )


@dataclass(frozen=True)
class State:
    kind: str
    label: str
    count: int


@dataclass(frozen=True)
class Intervals:
    """Frequency intervals over [0, 1): `states[i]` owns `[lower[i], lower[i] + width[i])`."""

    states: tuple[State, ...]
    lower: FloatArray
    width: FloatArray
    other_share: float

    @property
    def kept(self) -> int:
        """Number of emittable states (every state but a trailing `other`)."""
        return len(self.states) - (1 if self.other_share > 0 else 0)


def intervals_for(column: ColumnMarginal, role: str) -> Intervals:
    if role == ROLE_NULL:
        states = [
            State(STATE_PRESENT, "", column.non_null_count),
            State(STATE_NULL, "", column.null_count),
        ]
    else:
        states = [State(STATE_LABEL, label, count) for label, count in column.top_values]
        if column.null_count > 0:
            states.append(State(STATE_NULL, "", column.null_count))
    states.sort(key=lambda s: (-s.count, _KIND_RANK[s.kind], s.label))
    other = column.other_count if role != ROLE_NULL else 0
    if other > 0:
        states.append(State(STATE_OTHER, "", other))
    counts = np.array([s.count for s in states], dtype=np.float64)
    total = float(counts.sum())
    width = counts / total
    lower = np.concatenate(([0.0], np.cumsum(width)[:-1]))
    return Intervals(tuple(states), lower, width, other / total)


def encode_states(intervals: Intervals, state_index: IntArray, jitter: FloatArray) -> FloatArray:
    """Fit-side: a uniform point inside each row's state interval, kept strictly inside (0, 1)."""
    u = intervals.lower[state_index] + jitter * intervals.width[state_index]
    return np.clip(u, _UNIT_LOW, _UNIT_HIGH)


def decode_states(intervals: Intervals, u: FloatArray) -> IntArray:
    """Sample-side: the kept state whose interval contains `u * (1 - p_other)`."""
    scaled = np.asarray(u, dtype=np.float64) * (1.0 - intervals.other_share)
    kept = intervals.kept
    upper = (intervals.lower + intervals.width)[:kept]
    index = np.searchsorted(upper, scaled, side="right")
    return np.clip(index, 0, kept - 1).astype(np.int64)


def numeric_inverse(column: ColumnMarginal, u: FloatArray) -> FloatArray:
    """Histogram inverse CDF of `u` in [0, 1], uniform inside the bin, never in an empty bin."""
    edges = np.asarray(column.bin_edges, dtype=np.float64)
    counts = np.asarray(column.bin_counts, dtype=np.float64)
    cumulative = np.cumsum(counts)
    total = cumulative[-1]
    positive = np.flatnonzero(counts > 0)
    t = np.asarray(u, dtype=np.float64) * total
    k = np.searchsorted(cumulative, t, side="right")
    k = np.clip(k, positive[0], positive[-1])
    previous = np.where(k > 0, cumulative[k - 1], 0.0)
    fraction = np.clip((t - previous) / counts[k], 0.0, 1.0)
    low, high = edges[k], edges[k + 1]
    out: FloatArray = np.clip(low + fraction * (high - low), low, high)
    return out


def is_integer_dtype(dtype: str) -> bool:
    """Same predicate as `statistical/_sample._is_integer_dtype` (kept local: that module is private)."""
    return dtype.lstrip("uU").startswith("int") or dtype.startswith("Int")
