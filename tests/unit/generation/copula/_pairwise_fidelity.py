"""Pairwise fidelity between a real and a synthetic frame (test helper, not shipped).

Method follows SDMetrics' column-pair metrics (method only; SDMetrics is under the Business Source
License and is never imported):

- CorrelationSimilarity for two numeric columns: `1 - |r_real - r_synth| / 2`, each correlation
  on that frame's pairwise-complete rows; Pearson and Spearman are both reported.
- ContingencySimilarity for any pair with a categorical column: `1 - TVD` between the normalized
  joint frequency tables, null as its own category, numeric columns discretized into 10
  equal-width bins over the real column's range (synthetic values clipped into it).

Kind comes from the dtype: numeric non-bool dtypes are numeric, everything else categorical.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

_NULL = "\x00null"


@dataclass(frozen=True)
class PairScore:
    a: str
    b: str
    metric: str  # "pearson", "spearman" or "contingency"
    score: float | None


def _is_numeric(series: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)


def correlation_similarity(
    real: pd.DataFrame, synth: pd.DataFrame, a: str, b: str, method: str
) -> float | None:
    r = real[[a, b]].dropna().astype(float)
    s = synth[[a, b]].dropna().astype(float)
    if min(r[a].std(), r[b].std(), s[a].std(), s[b].std()) == 0 or len(r) < 2 or len(s) < 2:
        return None
    return 1.0 - abs(r[a].corr(r[b], method=method) - s[a].corr(s[b], method=method)) / 2.0


def _discrete(frame: pd.DataFrame, real: pd.DataFrame, column: str, bins: int) -> pd.Series:
    series = frame[column]
    if _is_numeric(real[column]):
        observed = real[column].dropna().astype(float)
        edges = np.histogram_bin_edges(observed, bins=bins)
        values = series.astype(float).clip(edges[0], edges[-1])
        index = np.clip(np.searchsorted(edges, values, side="right") - 1, 0, bins - 1)
        return pd.Series(np.where(series.isna(), _NULL, index.astype(str)), index=series.index)
    return series.map(
        lambda v: _NULL if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)
    )


def contingency_similarity(
    real: pd.DataFrame, synth: pd.DataFrame, a: str, b: str, bins: int = 10
) -> float:
    def table(frame: pd.DataFrame) -> pd.Series:
        keys = pd.DataFrame(
            {"a": _discrete(frame, real, a, bins), "b": _discrete(frame, real, b, bins)}
        )
        return keys.value_counts(normalize=True)

    diff = table(real).sub(table(synth), fill_value=0.0).abs().sum()
    return 1.0 - float(diff) / 2.0


def pairwise_fidelity(real: pd.DataFrame, synth: pd.DataFrame, bins: int = 10) -> list[PairScore]:
    if set(real.columns) != set(synth.columns):
        raise ValueError("pairwise_fidelity: the frames must have the same columns.")
    columns = list(real.columns)
    out: list[PairScore] = []
    for i, a in enumerate(columns):
        for b in columns[i + 1 :]:
            if _is_numeric(real[a]) and _is_numeric(real[b]):
                for method in ("pearson", "spearman"):
                    out.append(
                        PairScore(a, b, method, correlation_similarity(real, synth, a, b, method))
                    )
            else:
                out.append(
                    PairScore(a, b, "contingency", contingency_similarity(real, synth, a, b, bins))
                )
    return out
