"""Distance to Closest Record (DCR) for the SynthReport (D7b).

For each output row, the minimum Gower-style distance to any source row,
reported as aggregate percentiles only (per-row distances would leak which
source rows the synth is closest to). With a holdout frame it adds the
memorization-vs-generalization comparison:

  median(DCR_synth_to_source) ~= median(DCR_synth_to_holdout)
    -> synth is generalizing
  median(DCR_synth_to_source) << median(DCR_synth_to_holdout)
    -> synth is memorizing the training source

DCR is a sanity check, not a privacy guarantee. `synth_report.py` assembles
this block into the report and re-exports `compute_dcr`.

Pattern: DCR + holdout comparison follows SDV's
`NumericalRadiusNearestNeighbors` and `CategoricalCAP` privacy metrics
(sdv-dev/SDMetrics). Gower distance for the mixed-type case is Gower 1971
("A General Coefficient of Similarity and Some of its Properties"), the
standard cross-type distance.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def compute_dcr(
    source: pd.DataFrame,
    output: pd.DataFrame,
    *,
    holdout: pd.DataFrame | None = None,
    subset_columns: list[str] | None = None,
    sample_cap: int = 5000,
) -> dict[str, Any]:
    """Distance to Closest Record sanity check (D7b).

    For each output row, compute the minimum Gower distance to any
    source row. Returns aggregate percentiles of those minimums; the
    raw per-row distances are NOT included (per the security
    requirements, they would leak which source rows the synth is
    closest to).

    When `holdout` is provided, the same computation runs against
    the holdout frame, producing the memorization-vs-generalization
    comparison block. Operators read the comparison this way:

        median(synth_to_source) ~ median(synth_to_holdout)
          -> synth is generalizing (good privacy signal)
        median(synth_to_source) << median(synth_to_holdout)
          -> synth is memorizing the training source (privacy risk)

    Gower distance (Gower 1971):
      - numeric columns: |a - b| / (max - min), the range-normalized
        absolute difference. NaN handling: if either side is NaN the
        per-column distance is 1.0 (worst case) for that column.
      - categorical / object columns: 0 if equal, 1 otherwise.
      - per-row distance is the mean of per-column distances, so it
        lands in [0, 1].

    Cost: O(N_out * N_source * cols). The sample_cap is a defensive
    guard against pathological frames; both sides are independently
    capped to `sample_cap` rows by deterministic sample (head() is
    used so the same call returns the same DCR; np.random would
    inject hidden non-determinism into a privacy metric).

    Returns:

        {
          "metric": "distance_to_closest_record",
          "synth_to_source": {
            "median": float, "p05": ..., "p25": ..., "p75": ..., "p95": ...,
            "rows_sampled": int, "source_rows_sampled": int,
          },
          "synth_to_holdout": {...} | None,
          "comparison": {
            "median_ratio": float | None,   # source / holdout
            "interpretation": "generalizing" | "borderline" | "memorizing" | None,
          },
          "subset_columns": [str, ...],
          "warning": str | None,
        }
    """
    if source is None or output is None:
        return _dcr_unavailable("source or output not provided")

    out_n = len(output)
    src_n = len(source)
    if out_n == 0 or src_n == 0:
        return _dcr_unavailable("source or output is empty")

    if subset_columns is None:
        cols = sorted(set(source.columns) & set(output.columns))
    else:
        cols = [c for c in subset_columns if c in source.columns and c in output.columns]
    if not cols:
        return _dcr_unavailable("no overlapping columns between source and output")

    # Cap deterministically with head() so the same inputs always
    # produce the same DCR. Random sampling here would make the
    # metric non-reproducible, which defeats the purpose of a sanity
    # check (operator can't compare against a previous run).
    src_capped = source.head(sample_cap)
    out_capped = output.head(sample_cap)

    # Build the normalization spec from the source frame ONLY. The
    # output's range is irrelevant: we're measuring how close output
    # rows are to source rows, in the source's own units. Using the
    # holdout's range would also bias the comparison.
    normalizer = _build_gower_normalizer(src_capped, cols)

    synth_to_source = _gower_min_distances(
        out_capped,
        src_capped,
        cols,
        normalizer,
    )
    synth_to_source_block = _summarize_dcr(synth_to_source, len(src_capped))

    synth_to_holdout_block = None
    comparison = {"median_ratio": None, "interpretation": None}
    if holdout is not None and len(holdout) > 0:
        h_cols = [c for c in cols if c in holdout.columns]
        if h_cols:
            holdout_capped = holdout.head(sample_cap)
            synth_to_holdout = _gower_min_distances(
                out_capped,
                holdout_capped,
                h_cols,
                normalizer,
            )
            synth_to_holdout_block = _summarize_dcr(
                synth_to_holdout,
                len(holdout_capped),
            )
            comparison = _interpret_holdout_comparison(
                synth_to_source_block["median"],
                synth_to_holdout_block["median"],
            )

    return {
        "metric": "distance_to_closest_record",
        "synth_to_source": synth_to_source_block,
        "synth_to_holdout": synth_to_holdout_block,
        "comparison": comparison,
        "subset_columns": cols,
        "warning": (
            "DCR is a sanity check, not a privacy guarantee. "
            "A high DCR does not establish that synthetic rows "
            "cannot be linked back to source records."
        ),
    }


def _round(x: float) -> float:
    """Stable JSON-friendly rounding to 4 places."""
    return round(float(x), 4)


def _build_gower_normalizer(source: pd.DataFrame, cols: list[str]) -> dict[str, Any]:
    """Per-column metadata: numeric range or 'categorical' marker.

    Computed from the SOURCE frame's range only. Using the output
    or holdout range to normalize would let an outlier in either
    frame compress the meaningful source range and skew the metric.
    """
    spec: dict[str, Any] = {}
    for col in cols:
        ser = source[col]
        if pd.api.types.is_numeric_dtype(ser):
            arr = pd.to_numeric(ser, errors="coerce").dropna().to_numpy(dtype=float)
            if arr.size == 0:
                spec[col] = {"kind": "categorical"}
                continue
            lo = float(arr.min())
            hi = float(arr.max())
            spec[col] = {
                "kind": "numeric",
                "lo": lo,
                "hi": hi,
                "range": hi - lo if hi > lo else 1.0,
            }
        else:
            spec[col] = {"kind": "categorical"}
    return spec


def _gower_min_distances(
    output: pd.DataFrame,
    reference: pd.DataFrame,
    cols: list[str],
    normalizer: dict[str, Any],
) -> np.ndarray:
    """For each output row, the minimum Gower distance to any
    reference row, returned as a (len(output),) float array.

    Vectorized per column: each iteration builds an (N_out, N_ref)
    distance contribution for one column, sums into a running
    distance accumulator, then takes axis=1 min at the end. Memory:
    O(N_out * N_ref) for the accumulator (one column at a time).
    """
    n_out = len(output)
    n_ref = len(reference)
    if n_out == 0 or n_ref == 0 or not cols:
        return np.zeros(n_out, dtype=float)

    # Distance accumulator: sum of per-column distances; divide by
    # column count at the end for the Gower mean.
    dist_sum = np.zeros((n_out, n_ref), dtype=float)
    contributing_cols = 0

    for col in cols:
        spec = normalizer.get(col)
        if spec is None:
            continue
        if spec["kind"] == "numeric":
            r = spec["range"]
            # Coerce both sides; NaN per-cell gets max distance (1.0)
            # via the post-fill nan_to_num path below.
            out_vals = pd.to_numeric(output[col], errors="coerce").to_numpy(dtype=float)
            ref_vals = pd.to_numeric(reference[col], errors="coerce").to_numpy(dtype=float)
            # Broadcast subtraction: shape (n_out, 1) - shape (1, n_ref)
            # -> (n_out, n_ref). abs + divide by source range -> [0, 1]
            # (can exceed 1 if output has a value beyond source range;
            # clip to 1 so a single outlier doesn't dominate the mean).
            diff = np.abs(out_vals[:, None] - ref_vals[None, :]) / r
            diff = np.where(np.isnan(diff), 1.0, diff)
            np.clip(diff, 0.0, 1.0, out=diff)
            dist_sum += diff
            contributing_cols += 1
        else:
            # Categorical / object. astype(str) for stable equality
            # (handles None as 'None', NaN as 'nan' the same way
            # the row-hash path does).
            out_vals = output[col].astype(str).to_numpy()
            ref_vals = reference[col].astype(str).to_numpy()
            ne = out_vals[:, None] != ref_vals[None, :]
            dist_sum += ne.astype(float)
            contributing_cols += 1

    if contributing_cols == 0:
        return np.zeros(n_out, dtype=float)

    mean_dist = dist_sum / contributing_cols
    return mean_dist.min(axis=1)


def _summarize_dcr(distances: np.ndarray, ref_rows: int) -> dict[str, Any]:
    """Aggregate the per-row minimum distances. NO raw distances
    in the output - only percentiles + counts."""
    if distances.size == 0:
        return {
            "median": None,
            "p05": None,
            "p25": None,
            "p75": None,
            "p95": None,
            "rows_sampled": 0,
            "source_rows_sampled": ref_rows,
        }
    return {
        "median": _round(float(np.median(distances))),
        "p05": _round(float(np.quantile(distances, 0.05))),
        "p25": _round(float(np.quantile(distances, 0.25))),
        "p75": _round(float(np.quantile(distances, 0.75))),
        "p95": _round(float(np.quantile(distances, 0.95))),
        "rows_sampled": int(distances.size),
        "source_rows_sampled": ref_rows,
    }


def _interpret_holdout_comparison(
    median_source: float | None,
    median_holdout: float | None,
) -> dict[str, Any]:
    """Memorization signal from the source/holdout median ratio.

    The reasoning: if the synth was trained on `source` and has
    never seen `holdout`, then for a model that GENERALIZES the
    median DCR against both frames should be similar (the model is
    drawing from the underlying distribution, not the specific
    training rows). For a model that MEMORIZES, the synth rows
    are systematically closer to source rows than to holdout rows
    -> source-median < holdout-median.

    Thresholds:
      ratio >= 0.85   -> generalizing (DCRs roughly equal)
      0.50 <= ratio   -> borderline (some memorization signal)
      ratio < 0.50    -> memorizing  (synth markedly closer to source)

    These match SDV's documented thresholds for the same comparison.
    """
    if median_source is None or median_holdout is None:
        return {"median_ratio": None, "interpretation": None}
    if median_holdout == 0:
        # Holdout perfectly matched somewhere; ratio is undefined.
        # If source is also 0 the synth equally memorizes both,
        # which is its own kind of red flag; if source > 0 the
        # holdout is the leakier side, which means our training
        # data isn't what's leaking.
        return {"median_ratio": None, "interpretation": None}
    ratio = median_source / median_holdout
    if ratio >= 0.85:
        interp = "generalizing"
    elif ratio >= 0.50:
        interp = "borderline"
    else:
        interp = "memorizing"
    return {"median_ratio": _round(ratio), "interpretation": interp}


def _dcr_unavailable(reason: str) -> dict[str, Any]:
    return {
        "metric": "distance_to_closest_record",
        "synth_to_source": None,
        "synth_to_holdout": None,
        "comparison": {"median_ratio": None, "interpretation": None},
        "subset_columns": [],
        "warning": reason,
    }
