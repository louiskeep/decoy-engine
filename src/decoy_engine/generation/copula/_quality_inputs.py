"""Inputs of the copula quality report: argument checks, the tie between the real frame and the
model, the canonical form every frame is compared in, recoded discrete states, systematic samples.

The tie is the fit's own rule turned around: the fit accepts a snapshot only if a recomputed
snapshot of the frame equals it, and the report accepts `real` only if its recomputed snapshot,
with the parameters the model was fit with, gives the model's marginals.

Discrete states for contingency tables are recoded before tabulation: a state held by fewer than
`MIN_DISTINGUISHED_ROWS` real rows joins the column's `other` state, and a small `other` joins the
smallest remaining state (global recoding; Hundepool et al., Statistical Disclosure Control, Wiley
2012, sec. 3.4 and 4.2). Samples are systematic (Cochran, Sampling Techniques, 1977, ch. 8), so
no random stream is needed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pandas as pd

from decoy_engine.generation.copula import _floors
from decoy_engine.generation.copula._fit import _DEFAULT_BINS, _distinguished
from decoy_engine.generation.copula._marginals import from_snapshot_entry
from decoy_engine.generation.copula._model import (
    KIND_CATEGORICAL,
    KIND_EMPTY,
    KIND_NUMERIC,
    ColumnMarginal,
    GaussianCopulaModel,
)
from decoy_engine.quality.snapshot import compute_distribution_snapshot

IntArray = npt.NDArray[np.int64]

MAX_DCR_ROWS = 20000
_FREETEXT = "freetext"
_MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_PREFIX = "copula quality report"


def _is_numeric(series: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_arguments(
    real: object,
    synthetic: object,
    model: object,
    holdout: object,
    dcr_max_rows: object,
    attack_extras: object,
) -> None:
    """Every refusal that needs no statistic. Messages name an argument or a column and the
    rule, never a value."""
    if not isinstance(model, GaussianCopulaModel):
        raise TypeError(f"{_PREFIX}: model must be a GaussianCopulaModel.")
    frames = {"real": real, "synthetic": synthetic}
    if holdout is not None:
        frames["holdout"] = holdout
    for what, frame in frames.items():
        if not isinstance(frame, pd.DataFrame):
            raise TypeError(f"{_PREFIX}: {what} must be a pandas DataFrame.")
    if not _is_int(dcr_max_rows):
        raise TypeError(f"{_PREFIX}: dcr_max_rows must be an int.")
    if not _floors.MIN_FIT_ROWS <= dcr_max_rows <= MAX_DCR_ROWS:  # type: ignore[operator]
        raise ValueError(
            f"{_PREFIX}: dcr_max_rows must be between {_floors.MIN_FIT_ROWS} and {MAX_DCR_ROWS}."
        )
    if attack_extras is not None:
        if not isinstance(attack_extras, str):
            raise TypeError(f"{_PREFIX}: attack_extras must be a module name or None.")
        if _MODULE_NAME.fullmatch(attack_extras) is None:
            raise ValueError(f"{_PREFIX}: attack_extras must be a dotted module name.")
    for what, frame in frames.items():
        _check_frame(what, cast(pd.DataFrame, frame), model)
    if len(real) != model.row_count:  # type: ignore[arg-type]
        raise ValueError(f"{_PREFIX}: real does not have the model's row count.")
    if len(synthetic) == 0:  # type: ignore[arg-type]
        raise ValueError(f"{_PREFIX}: synthetic must have at least one row.")
    if holdout is not None and len(holdout) < _floors.MIN_FIT_ROWS:  # type: ignore[arg-type]
        raise ValueError(f"{_PREFIX}: holdout needs at least {_floors.MIN_FIT_ROWS} rows.")


def _check_frame(what: str, frame: pd.DataFrame, model: GaussianCopulaModel) -> None:
    names = [str(c) for c in frame.columns]
    if len(set(names)) != len(names):
        raise ValueError(f"{_PREFIX}: {what} column names must be unique after str().")
    if set(names) != {c.name for c in model.columns}:
        raise ValueError(f"{_PREFIX}: {what} columns must be exactly the model's columns.")
    frame = frame.set_axis(names, axis=1)
    for column in model.columns:
        series = frame[column.name]
        if column.kind != KIND_EMPTY and (column.kind == KIND_NUMERIC) != _is_numeric(series):
            raise ValueError(
                f"{_PREFIX}: {what} column {column.name!r} dtype does not match the model kind."
            )
        if column.kind == KIND_NUMERIC:
            values = series.dropna().to_numpy(dtype=np.float64)
            if not np.all(np.isfinite(values)):
                raise ValueError(f"{_PREFIX}: {what} column {column.name!r} has an infinite value.")


def with_str_names(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.set_axis([str(c) for c in frame.columns], axis=1)


def _parameters(model: GaussianCopulaModel) -> tuple[int, int | None]:
    """Bin count and top-K the model's snapshot was computed with (as `_fit._snapshot_parameters`
    infers them from a snapshot). Top-K is None when no column was collapsed."""
    bins = {
        len(c.bin_edges) - 1
        for c in model.columns
        if c.kind == KIND_NUMERIC and len(c.bin_edges) >= 2 and c.bin_edges[0] != c.bin_edges[-1]
    }
    collapsed = {
        len(c.top_values) for c in model.columns if c.kind == KIND_CATEGORICAL and c.other_count
    }
    if len(bins) > 1 or len(collapsed) > 1:
        raise ValueError(f"{_PREFIX}: model columns disagree on the bin count or top-K.")
    return (bins.pop() if bins else _DEFAULT_BINS), (collapsed.pop() if collapsed else None)


def model_snapshot(
    frame: pd.DataFrame, model: GaussianCopulaModel, high_cardinality: frozenset[str]
) -> dict[str, Any]:
    """The snapshot of `frame` with the parameters the model's snapshot was computed with."""
    bins, top_k = _parameters(model)
    longest = max([len(c.top_values) for c in model.columns if c.kind == KIND_CATEGORICAL] + [1])
    return compute_distribution_snapshot(
        frame,
        numeric_bins=bins,
        categorical_top_k=top_k if top_k is not None else longest,
        high_cardinality_columns=sorted(high_cardinality),
    )


def tie_to_model(
    real: pd.DataFrame, model: GaussianCopulaModel
) -> tuple[dict[str, Any], frozenset[str]]:
    """Recompute the real snapshot with the model's parameters and require the model's marginals.
    Returns the snapshot and the columns held with their full vocabulary (high cardinality)."""
    _, top_k = _parameters(model)
    # A full-vocabulary column longer than a collapsed column's top-K: the model can only hold it
    # through the snapshot's high-cardinality path.
    high = frozenset(
        c.name
        for c in model.columns
        if c.kind == KIND_CATEGORICAL
        and c.other_count == 0
        and top_k is not None
        and len(c.top_values) > top_k
    )
    snapshot = model_snapshot(real, model, high)
    # Past the snapshot's distinct-value cliff only a high-cardinality column stays categorical.
    crossed = frozenset(
        c.name
        for c in model.columns
        if c.kind == KIND_CATEGORICAL and snapshot["columns"][c.name]["kind"] == _FREETEXT
    )
    if crossed:
        high |= crossed
        snapshot = model_snapshot(real, model, high)
    entries: Mapping[str, Any] = snapshot["columns"]
    for column in model.columns:
        entry = entries[column.name]
        if entry.get("kind") not in (KIND_NUMERIC, KIND_CATEGORICAL, KIND_EMPTY):
            raise ValueError(
                f"{_PREFIX}: real column {column.name!r} kind does not match the model."
            )
        recomputed = from_snapshot_entry(column.name, entry)
        for field in (
            "kind", "dtype", "null_count", "non_null_count", "bin_edges", "bin_counts",
            "top_values", "other_count",
        ):  # fmt: skip
            if getattr(recomputed, field) != getattr(column, field):
                raise ValueError(
                    f"{_PREFIX}: real {field} for column {column.name!r} does not match the model."
                )
    return snapshot, high


def canonical(frame: pd.DataFrame, model: GaussianCopulaModel) -> pd.DataFrame:
    """A new frame in model column order: numeric as float64 with NaN for null, categorical as
    `str(value)` (the snapshot's label rule) or None. The input is never modified."""
    frame = with_str_names(frame)
    out: dict[str, pd.Series] = {}
    for column in model.columns:
        series = frame[column.name]
        if column.kind == KIND_NUMERIC:
            values = series.astype("Float64").to_numpy(dtype=np.float64, na_value=np.nan)
            out[column.name] = pd.Series(values, dtype=np.float64)
        else:
            missing = series.isna().to_numpy()
            labels = series.astype(str).to_numpy(dtype=object)
            labels[missing] = None
            out[column.name] = pd.Series(labels, dtype=object)
    return pd.DataFrame(out)


def _raw_states(series: pd.Series, column: ColumnMarginal) -> tuple[IntArray, int]:
    """State codes before recoding and the number of codes. Numeric: bins 0..B-1, null B.
    Categorical: labels 0..L-1 in model order, null L, any other label L + 1."""
    missing = series.isna().to_numpy()
    if column.kind == KIND_NUMERIC:
        edges = np.asarray(column.bin_edges, dtype=np.float64)
        bins = len(column.bin_counts)
        values = series.to_numpy(dtype=np.float64)
        # numpy histogram bins are [e_k, e_k+1) with the last closed; values off the rounded
        # edges clip into the end bins.
        index = np.clip(
            np.searchsorted(edges, np.nan_to_num(values), side="right") - 1, 0, bins - 1
        )
        return np.where(missing, bins, index).astype(np.int64), bins + 1
    by_label = {label: i for i, (label, _) in enumerate(column.top_values)}
    size = len(column.top_values)
    codes = np.array(
        [
            size if m else by_label.get(v, size + 1)
            for m, v in zip(missing, series.to_numpy(), strict=True)
        ],
        dtype=np.int64,
    )
    return codes, size + 2


def discrete_states(
    real: pd.Series, synthetic: pd.Series, column: ColumnMarginal
) -> tuple[IntArray, IntArray]:
    """Recoded state codes for both frames, decided from real counts only (canonical series)."""
    real_raw, size = _raw_states(real, column)
    synth_raw, _ = _raw_states(synthetic, column)
    counts = np.bincount(real_raw, minlength=size)
    floor = _floors.MIN_DISTINGUISHED_ROWS
    other = size  # one more code: the merged small states
    mapping = np.where(counts < floor, other, np.arange(size))
    other_rows = int(counts[counts < floor].sum())
    kept = [i for i in range(size) if counts[i] >= floor]
    if 0 < other_rows < floor and kept:
        # A small merged group is still a small group: join it to the smallest remaining state.
        smallest = min(kept, key=lambda i: (counts[i], -i))
        mapping = np.where(mapping == other, smallest, mapping)
    return mapping[real_raw], mapping[synth_raw]


def distinguished(codes: IntArray) -> int:
    """Rows outside the largest group, the fit's own floor count."""
    return _distinguished(codes)


def value_groups(values: npt.NDArray[np.float64]) -> IntArray:
    """Tied-value groups of observed numeric values, as the fit groups a value variable."""
    return np.unique(values, return_inverse=True)[1].astype(np.int64).ravel()


def sample_rows(n: int, k: int) -> IntArray:
    """Systematic sample of k row positions out of n: floor(i n / k)."""
    if n <= k:
        return np.arange(n, dtype=np.int64)
    return (np.arange(k, dtype=np.int64) * n) // k
