"""Distribution-fidelity invariants for generate tables (Phase 1 / MEDIUM-2).

Extracted from _distribution.py (which was 655 LOC, over the 600-line cap) to
keep both modules within the size limit. _distribution.py re-exports
check_distribution_generate from here so existing callers are unaffected.

Implements plan section 6.3: config-derived baseline for generate tables.
No source frame is available for generate tables; the baseline is the
declared column weights and params.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from ._spec import ColumnDistributionSpec


def check_distribution_generate(
    job_name: str,
    table: str,
    spec: list[ColumnDistributionSpec],
    output_df: pd.DataFrame,
    config_table: dict[str, Any],
) -> None:
    """Assert distribution fidelity for one generated table.

    Generate tables have no source frame; the baseline is the configured
    weights / params (OWNER DECISION Q3: config-derived only, no committed
    golden snapshots). Checks:

    - Categorical generate columns: TVD between output value-frequency vector
      and declared weights <= col_spec.tolerance (default 0.05). At multi-
      thousand rows, sampling noise is small enough that this is meaningful.

    - Statistical numeric generate columns: assert output mean within
      col_spec.tolerance * max(|declared_mean|, 1.0) of declared mean, and
      output std within col_spec.tolerance * max(declared_std, 1.0) of
      declared std.

    - Formula generate columns carrying params.mean/std metadata: same
      mean/std band check as statistical (the formula type accepts an
      arbitrary Python expression; when the operator knows the output
      distribution they declare params as a reviewable contract).

    Args:
        job_name: Job name for error messages.
        table: Table name for error messages.
        spec: ColumnDistributionSpec entries for this table.
        output_df: Post-generate pandas DataFrame.
        config_table: Raw pipeline config dict for this table. Must carry
            generate_columns (list of dicts with name, type, and type-specific
            params like weights or params.mean/std).

    Raises:
        AssertionError: If any column's output distribution deviates from its
            declared weights / params beyond tolerance.
    """
    spec_by_col = {s.column: s for s in spec}
    gen_cols: list[dict[str, Any]] = config_table.get("generate_columns", [])

    for gen_col in gen_cols:
        if not isinstance(gen_col, dict):
            continue
        col_name = gen_col.get("name")
        if not isinstance(col_name, str) or col_name not in output_df.columns:
            continue
        col_spec = spec_by_col.get(col_name)
        tol = col_spec.tolerance if col_spec is not None else 0.05
        col_type = str(gen_col.get("type", ""))

        if col_type == "categorical":
            raw_weights = gen_col.get("weights") or {}
            categories: list[Any] = gen_col.get("categories") or []
            # Weights may be a list (parallel to categories, engine format) or a
            # dict (name -> weight, invariant-check format). Normalise to dict so
            # the TVD comparison has stable category names as keys.
            if isinstance(raw_weights, list):
                weights: dict[str, float] = {
                    str(c): float(w) for c, w in zip(categories, raw_weights, strict=True)
                }
            else:
                weights = {k: float(v) for k, v in (raw_weights or {}).items()}
            if not weights:
                continue
            weight_sum = sum(weights.values())
            if weight_sum <= 0:
                continue
            norm_weights = {k: v / weight_sum for k, v in weights.items()}
            n_total = len(output_df)
            if n_total == 0:
                continue
            out_freq = output_df[col_name].value_counts(normalize=True).to_dict()
            all_keys = set(norm_weights) | set(out_freq)
            tvd = 0.5 * sum(
                abs(norm_weights.get(k, 0.0) - float(out_freq.get(k, 0.0))) for k in all_keys
            )
            if tvd > tol:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] generate categorical TVD: "
                    f"tvd={tvd:.4f} > tolerance={tol} "
                    f"(declared weights vs output frequencies). "
                    f"Declared: {norm_weights}. "
                    f"Output: { {k: round(float(v), 4) for k, v in out_freq.items()} }."
                )

        elif col_type == "statistical":
            params: dict[str, Any] = gen_col.get("params") or {}
            declared_mean = params.get("mean")
            declared_std = params.get("std")
            if declared_mean is None or declared_std is None:
                continue
            series = output_df[col_name].dropna()
            if len(series) == 0:
                continue
            out_mean = float(series.mean())
            out_std = float(series.std())
            d_mean = float(declared_mean)
            d_std = float(declared_std)
            mean_band = tol * max(abs(d_mean), 1.0)
            std_band = tol * max(d_std, 1.0)
            if abs(out_mean - d_mean) > mean_band:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] generate statistical mean: "
                    f"out_mean={out_mean:.4f}, declared_mean={d_mean}, "
                    f"band={mean_band:.4f} (tol={tol}). "
                    f"Output mean outside declared parameter band."
                )
            if abs(out_std - d_std) > std_band:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] generate statistical std: "
                    f"out_std={out_std:.4f}, declared_std={d_std}, "
                    f"band={std_band:.4f} (tol={tol}). "
                    f"Output std outside declared parameter band."
                )

        else:
            # For any non-categorical, non-statistical type (e.g. formula), check
            # mean and std if the column config carries them as `params` metadata.
            # The formula type accepts an arbitrary Python expression; when the
            # operator knows the output distribution (e.g. gauss(mu, sigma)), they
            # declare params: {mean: mu, std: sigma} as a reviewable contract and
            # this branch enforces it.
            params_meta: dict[str, Any] = gen_col.get("params") or {}
            fm_mean = params_meta.get("mean")
            fm_std = params_meta.get("std")
            if fm_mean is None or fm_std is None:
                continue
            fm_series = output_df[col_name].dropna()
            if len(fm_series) == 0:
                continue
            fm_out_mean = float(fm_series.mean())
            fm_out_std = float(fm_series.std())
            fm_d_mean = float(fm_mean)
            fm_d_std = float(fm_std)
            fm_mean_band = tol * max(abs(fm_d_mean), 1.0)
            fm_std_band = tol * max(fm_d_std, 1.0)
            if abs(fm_out_mean - fm_d_mean) > fm_mean_band:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] generate {col_type} mean: "
                    f"out_mean={fm_out_mean:.4f}, declared_mean={fm_d_mean}, "
                    f"band={fm_mean_band:.4f} (tol={tol}). "
                    f"Output mean outside declared parameter band."
                )
            if abs(fm_out_std - fm_d_std) > fm_std_band:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] generate {col_type} std: "
                    f"out_std={fm_out_std:.4f}, declared_std={fm_d_std}, "
                    f"band={fm_std_band:.4f} (tol={tol}). "
                    f"Output std outside declared parameter band."
                )
