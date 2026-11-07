"""Fit a Gaussian copula: snapshot marginals plus a released latent correlation matrix.

Pattern: Gaussian copula with rank-based normal scores (Nelsen, An Introduction to Copulas, 2006;
Patki et al., "The Synthetic Data Vault", IEEE DSAA 2016, method only). See:
docs/plans/2026-11-07-v2-4.2-4.3-gaussian-copula.md.

The marginals are the caller's `distribution-snapshot/v1` entries, checked against a recomputed
snapshot of the same frame; only the correlation is computed here, so a differentially private
fit can later replace exactly that release. Latent scores, which never leave this module:

- numeric values: average-tie ranks over (n_observed + 1) through the inverse normal CDF, the
  semiparametric pseudo-observation estimator (Genest, Ghoudi, Rivest, Biometrika 1995; the van
  der Waerden normal-scores correlation, Klaassen and Wellner, Bernoulli 1997);
- categorical states and numeric null indicators: a seeded uniform point inside the row's
  frequency interval (RDT `UniformEncoder` / `NullTransformer(from_column)`, method only), also
  through the inverse normal CDF, because sampling feeds `ndtr(Z)` to the same intervals.

Correlations are Pearson on pairwise-complete rows. The disclosure floors (`_floors.py`) suppress
whole latent variables (an identity row and column), never single cells, so the repair of the
remaining block equals the repair of a fit without them. The matrix is repaired to the nearest
correlation matrix (Higham 2002) with an eigenvalue floor that survives rounding the release to
`RELEASE_DECIMALS`, then shrunk toward the identity only if still needed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from decoy_engine.generation.copula import _floors, _rng
from decoy_engine.generation.copula._marginals import (
    STATE_LABEL,
    STATE_NULL,
    STATE_OTHER,
    STATE_PRESENT,
    encode_states,
    from_snapshot_entry,
    intervals_for,
)
from decoy_engine.generation.copula._model import (
    KIND_CATEGORICAL,
    KIND_NUMERIC,
    ROLE_NULL,
    ROLE_VALUE,
    ColumnMarginal,
    GaussianCopulaModel,
    LatentVariable,
    Suppression,
    latent_layout,
    lower_triangle,
)
from decoy_engine.generation.copula._nearest_corr import nearest_correlation, shrink_to_definite
from decoy_engine.generation.copula._normal import ndtri
from decoy_engine.quality.snapshot import (
    DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION,
    compute_distribution_snapshot,
)

FloatArray = npt.NDArray[np.float64]

_DEFAULT_BINS = 10
_MARGINAL_FIELDS = ("dtype", "kind", "null_count", "non_null_count")
_STATS_FIELDS = ("bin_edges", "bin_counts", "top_values", "other_count", "high_cardinality")


def fit_gaussian_copula(
    frame: pd.DataFrame, snapshot: Mapping[str, Any], *, seed: bytes
) -> GaussianCopulaModel:
    """Fit a copula to `frame` whose marginals are the entries of `snapshot` (the frame's own
    `distribution-snapshot/v1`). Deterministic in (frame including row order, snapshot, seed)."""
    try:
        _rng.check_seed(seed)
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("copula: frame must be a pandas DataFrame.")
        names = [str(c) for c in frame.columns]
        if len(set(names)) != len(names):
            raise ValueError("copula: column names must be unique after conversion to str.")
        if len(frame) < _floors.MIN_FIT_ROWS:
            raise ValueError(f"copula: a fit needs at least {_floors.MIN_FIT_ROWS} rows.")
        frame = frame.set_axis(names, axis=1)
        _check_finite_numerics(frame)
        entries = _check_snapshot_matches(frame, snapshot)
        columns = tuple(from_snapshot_entry(name, entries[name]) for name in names)
        layout = latent_layout(columns)
        dimension = len(layout)
        if dimension > _floors.MAX_LATENT:
            raise ValueError(f"copula: more than {_floors.MAX_LATENT} latent variables.")
        if dimension * (dimension - 1) // 2 > _floors.max_latent_pairs(len(frame)):
            raise ValueError("copula: more latent pairs than half the row count; fit more rows.")

        scores = latent_scores(frame, columns, layout, _rng.stream(seed, _rng.FIT_LABEL))
        stats = _PairStats(scores)
        suppressed = suppression(stats, columns, layout)
        raw = raw_correlation(stats, layout)
        released = release(
            raw, [i for i in range(dimension) if i not in {s.variable for s in suppressed}]
        )
        return GaussianCopulaModel(len(frame), columns, lower_triangle(released), suppressed)
    except BaseException:
        del seed
        raise


def _check_finite_numerics(frame: pd.DataFrame) -> None:
    for name in frame.columns:
        series = frame[name]
        if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
            values = series.dropna().to_numpy(dtype=np.float64)
            if not np.all(np.isfinite(values)):
                raise ValueError(f"copula: numeric column {name!r} has an infinite value.")


def _check_snapshot_matches(frame: pd.DataFrame, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Require `snapshot` to be this frame's v1 snapshot: recompute it and compare every marginal
    field. Messages name the column and field, never a value."""
    if not isinstance(snapshot, Mapping):
        raise TypeError("copula: snapshot must be a mapping.")
    if snapshot.get("schema_version") != DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(f"copula: snapshot must be {DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION}.")
    entries = snapshot.get("columns")
    if not isinstance(entries, Mapping) or set(entries) != set(frame.columns):
        raise ValueError("copula: snapshot columns must be exactly the frame's columns.")
    if snapshot.get("row_count") != len(frame):
        raise ValueError("copula: snapshot row_count does not match the frame.")
    bins, top_k, high_cardinality = _snapshot_parameters(entries)
    expected = compute_distribution_snapshot(
        frame,
        numeric_bins=bins,
        categorical_top_k=top_k,
        high_cardinality_columns=high_cardinality,
    )["columns"]
    for name in frame.columns:
        given, wanted = entries[name], expected[name]
        if not isinstance(given, Mapping):
            raise TypeError(f"copula: snapshot entry for column {name!r} must be a mapping.")
        for field in _MARGINAL_FIELDS:
            if given.get(field) != wanted.get(field):
                raise ValueError(
                    f"copula: snapshot {field} for column {name!r} does not match the frame."
                )
        given_stats, wanted_stats = given.get("stats") or {}, wanted.get("stats") or {}
        for field in _STATS_FIELDS:
            if given_stats.get(field) != wanted_stats.get(field):
                raise ValueError(
                    f"copula: snapshot {field} for column {name!r} does not match the frame."
                )
    return dict(entries)


def _snapshot_parameters(entries: Mapping[str, Any]) -> tuple[int, int, list[str]]:
    """Infer the bin count and top-K a snapshot was computed with, so it can be recomputed."""
    bins: set[int] = set()
    collapsed: set[int] = set()
    lengths = [1]
    high_cardinality: list[str] = []
    for name, entry in entries.items():
        stats = (entry.get("stats") if isinstance(entry, Mapping) else None) or {}
        edges = stats.get("bin_edges") or []
        if entry.get("kind") == KIND_NUMERIC and len(edges) >= 2 and edges[0] != edges[-1]:
            bins.add(len(edges) - 1)
        if entry.get("kind") == KIND_CATEGORICAL:
            if stats.get("high_cardinality") is True:
                high_cardinality.append(str(name))
                continue
            top = stats.get("top_values") or []
            lengths.append(len(top))
            if stats.get("other_count"):
                collapsed.add(len(top))
    if len(bins) > 1 or len(collapsed) > 1:
        raise ValueError("copula: snapshot columns disagree on the bin count or top-K.")
    return (
        (bins.pop() if bins else _DEFAULT_BINS),
        (collapsed.pop() if collapsed else max(lengths)),
        high_cardinality,
    )


def latent_scores(
    frame: pd.DataFrame,
    columns: tuple[ColumnMarginal, ...],
    layout: tuple[LatentVariable, ...],
    bitgen: _rng.BitGenerator,
) -> FloatArray:
    """Normal scores, one column per latent variable, NaN where the variable is unobserved."""
    n = len(frame)
    out = np.full((n, len(layout)), np.nan)
    for j, variable in enumerate(layout):
        column = columns[variable.column]
        series = frame[column.name]
        if variable.role == ROLE_VALUE:
            _check_kind_matches_dtype(column, series)
            observed = series.notna().to_numpy()
            values = pd.to_numeric(series[observed]).astype(np.float64)
            ranks = values.rank(method="average").to_numpy()
            out[observed, j] = ndtri(ranks / (observed.sum() + 1.0))
            continue
        intervals = intervals_for(column, variable.role)
        if variable.role == ROLE_NULL:
            state = _null_indicator_states(intervals.states, series)
        else:
            _check_kind_matches_dtype(column, series)
            state = _categorical_states(intervals.states, series, column.name)
        out[:, j] = ndtri(encode_states(intervals, state, _rng.uniforms(bitgen, n)))
    return out


def _check_kind_matches_dtype(column: ColumnMarginal, series: pd.Series) -> None:
    numeric = pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)
    if (column.kind == KIND_NUMERIC) != numeric:
        raise ValueError(f"copula: column {column.name!r} kind does not match its dtype.")


def _null_indicator_states(states: tuple[Any, ...], series: pd.Series) -> npt.NDArray[np.int64]:
    present = next(i for i, s in enumerate(states) if s.kind == STATE_PRESENT)
    null = next(i for i, s in enumerate(states) if s.kind == STATE_NULL)
    return np.where(series.isna().to_numpy(), null, present).astype(np.int64)


def _categorical_states(
    states: tuple[Any, ...], series: pd.Series, name: str
) -> npt.NDArray[np.int64]:
    by_label = {s.label: i for i, s in enumerate(states) if s.kind == STATE_LABEL}
    null = next((i for i, s in enumerate(states) if s.kind == STATE_NULL), -1)
    other = next((i for i, s in enumerate(states) if s.kind == STATE_OTHER), -1)
    missing = series.isna().to_numpy()
    # The snapshot labels a category by str(value); the same conversion finds its state here.
    labels = series.astype(str).to_numpy()
    out = np.empty(len(series), dtype=np.int64)
    for i in range(len(series)):
        if missing[i]:
            out[i] = null
        else:
            out[i] = by_label.get(labels[i], other)
    if (out < 0).any():
        raise ValueError(f"copula: column {name!r} has a label outside its marginal and no tail.")
    return out


class _PairStats:
    """Pairwise-complete counts, Pearson correlations and per-side variances of the latent scores."""

    def __init__(self, scores: FloatArray) -> None:
        mask = (~np.isnan(scores)).astype(np.float64)
        x = np.nan_to_num(scores)
        self.observed = mask.sum(axis=0)
        self.shared = mask.T @ mask
        safe = np.maximum(self.shared, 1.0)
        sum_x = x.T @ mask  # [i, j]: sum of x_i over rows where j is observed
        sum_xx = (x * x).T @ mask
        cross = x.T @ x
        cov = cross - sum_x * sum_x.T / safe
        self.variance = np.maximum(sum_xx - sum_x * sum_x / safe, 0.0)  # [i, j]: var of i on shared
        denom = np.sqrt(self.variance * self.variance.T)
        with np.errstate(invalid="ignore", divide="ignore"):
            corr = np.where(denom > 0, cov / np.where(denom > 0, denom, 1.0), 0.0)
        self.correlation = np.clip(corr, -1.0, 1.0)


def suppression(
    stats: _PairStats, columns: tuple[ColumnMarginal, ...], layout: tuple[LatentVariable, ...]
) -> tuple[Suppression, ...]:
    """Which latent variables the floors withhold, decided before any correlation is released."""
    reasons: dict[int, str] = {}
    for j, variable in enumerate(layout):
        column = columns[variable.column]
        if variable.role == ROLE_VALUE:
            if stats.variance[j, j] <= 0:
                reasons[j] = _floors.SUPPRESS_ZERO_VARIANCE
            elif len(column.bin_counts) > column.non_null_count // _floors.ROWS_PER_BIN:
                reasons[j] = _floors.SUPPRESS_ROWS_PER_BIN
        else:
            counts = [s.count for s in intervals_for(column, variable.role).states]
            if sum(counts) - max(counts) < _floors.MIN_DISTINGUISHED_ROWS:
                reasons[j] = _floors.SUPPRESS_DISTINGUISHED_ROWS
    for i in range(len(layout)):
        for j in range(i + 1, len(layout)):
            if i in reasons or j in reasons or _structural(layout, i, j):
                continue
            if stats.shared[i, j] < _floors.MIN_PAIR_ROWS:
                sparser = i if stats.observed[i] < stats.observed[j] else j
                reasons[sparser] = _floors.SUPPRESS_PAIR_ROWS
            elif stats.variance[j, i] <= 0 or stats.variance[i, j] <= 0:
                reasons[j if stats.variance[j, i] <= 0 else i] = _floors.SUPPRESS_ZERO_VARIANCE
    return tuple(Suppression(v, reasons[v]) for v in sorted(reasons))


def _structural(layout: tuple[LatentVariable, ...], i: int, j: int) -> bool:
    """A value and its own null indicator: the indicator is constant where the value is observed."""
    return layout[i].column == layout[j].column


def raw_correlation(stats: _PairStats, layout: tuple[LatentVariable, ...]) -> FloatArray:
    """Pairwise-complete estimates with a unit diagonal and the structural zeros; suppressed
    variables are dropped later by `release`, which repairs only the kept block."""
    d = len(layout)
    raw = stats.correlation.copy()
    for i in range(d):
        for j in range(d):
            if i == j:
                raw[i, j] = 1.0
            elif _structural(layout, i, j):
                raw[i, j] = 0.0
    return raw


def _round_release(matrix: FloatArray) -> FloatArray:
    out = np.round(matrix, _floors.RELEASE_DECIMALS) + 0.0
    np.fill_diagonal(out, 1.0)
    return out


def release(raw: FloatArray, kept: list[int]) -> FloatArray:
    """Repair the kept block, embed it in the identity, round, and shrink only if still needed."""
    d = raw.shape[0]
    full = np.eye(d)
    if kept:
        block = raw[np.ix_(kept, kept)]
        full[np.ix_(kept, kept)] = nearest_correlation(
            block, floor=_floors.rounding_floor(len(kept))
        )
    return shrink_to_definite(full, _round_release, _floors.MIN_EIGENVALUE)
