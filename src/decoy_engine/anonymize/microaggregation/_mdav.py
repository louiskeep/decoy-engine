"""MDAV partition of standardised records into groups of k to 2k-1.

Pattern: MDAV fixed-size microaggregation (Domingo-Ferrer and Torra 2005)

J. Domingo-Ferrer and V. Torra, "Ordinal, continuous and heterogeneous
k-anonymity through microaggregation", Data Mining and Knowledge Discovery
11(2), 2005; the concrete choices follow sdcMicro's ``src/Mdav.cpp``
(OECD, LGPL-2.1; the sdcMicro package is GPL-2), read for behaviour, no code
copied:

- variables are weighted by ``1 / variance``, i.e. squared Euclidean distance
  on standardised data;
- while at least 2k records remain: take the centroid ``c``, the record
  ``x_r`` farthest from it and the record ``x_s`` farthest from ``x_r``; group
  ``x_r`` with its k-1 nearest; if the pass began with at least 3k records,
  group the k records nearest ``x_s`` (chosen before the first group was
  removed) among the rest;
- fewer than 2k left: they form the last group (k to 2k-1 records).

Ties go to the earliest row: remaining rows are kept in ascending row order,
``argmax`` returns the first maximum, and the k nearest take the earliest rows
at the cut-off distance. Distances are float64 (sdcMicro uses float32).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

FloatMatrix = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]


def standardise(x: FloatMatrix, names: Sequence[str]) -> tuple[FloatMatrix, BoolArray]:
    """Return ``(z, constant)``: z-scores of the non-constant columns and the constant mask.

    Constancy is decided exactly (every value equal to the first), not by a zero
    standard deviation, which float rounding makes non-zero for values such as 0.1.
    Centring keeps offset columns (epoch seconds) from losing digits to the offset.
    """
    rows = x.shape[0]
    constant = np.array(
        [rows == 0 or bool((x[:, j] == x[0, j]).all()) for j in range(x.shape[1])], dtype=bool
    )
    kept = x[:, ~constant]
    with np.errstate(over="ignore", invalid="ignore"):
        mean = kept.mean(axis=0) if rows else np.zeros(kept.shape[1])
        std = kept.std(axis=0) if rows else np.ones(kept.shape[1])
        z = (kept - mean) / std
    bad = ~(np.isfinite(mean) & np.isfinite(std) & (std > 0) & np.isfinite(z).all(axis=0))
    if bad.any():
        name = [n for n, c in zip(names, constant, strict=True) if not c][int(np.argmax(bad))]
        raise ValueError(f"column {name!r} is too large to standardise")
    return np.ascontiguousarray(z), constant


def _k_nearest(distances: npt.NDArray[np.float64], k: int) -> IntArray:
    """Positions of the k smallest distances; at the cut-off, the earliest positions."""
    cut = np.partition(distances, k - 1)[k - 1]
    below = np.flatnonzero(distances < cut)
    at_cut = np.flatnonzero(distances == cut)[: k - below.size]
    return np.sort(np.concatenate([below, at_cut]))


def _squared_distances(z: FloatMatrix, point: FloatMatrix) -> npt.NDArray[np.float64]:
    diff = z - point
    return (diff * diff).sum(axis=1)


def _emit(
    z: FloatMatrix,
    rows: IntArray,
    distances: npt.NDArray[np.float64],
    k: int,
    groups: IntArray,
    group_id: int,
) -> tuple[FloatMatrix, IntArray]:
    chosen = _k_nearest(distances, k)
    groups[rows[chosen]] = group_id
    keep = np.ones(rows.size, dtype=bool)
    keep[chosen] = False
    return z[keep], rows[keep]


def mdav_groups(z: FloatMatrix, k: int) -> IntArray:
    """Group id per row, numbered in the order groups are formed."""
    rows: IntArray = np.arange(z.shape[0], dtype=np.int64)
    groups = np.empty(z.shape[0], dtype=np.int64)
    next_id = 0
    while rows.size >= 2 * k:
        two_groups = rows.size >= 3 * k
        x_r = int(np.argmax(_squared_distances(z, z.mean(axis=0))))
        from_r = _squared_distances(z, z[x_r])
        # x_s is picked before the first group leaves, as sdcMicro does.
        anchor = z[int(np.argmax(from_r))].copy() if two_groups else None
        z, rows = _emit(z, rows, from_r, k, groups, next_id)
        next_id += 1
        if anchor is not None:
            z, rows = _emit(z, rows, _squared_distances(z, anchor), k, groups, next_id)
            next_id += 1
    if rows.size:
        groups[rows] = next_id
    return groups
