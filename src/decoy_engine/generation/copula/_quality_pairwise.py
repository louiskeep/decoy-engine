"""Pairwise fidelity between a real and a synthetic table, gated pair by pair.

Pattern: SDMetrics column-pair similarity (CorrelationSimilarity, ContingencySimilarity; sdv-dev
SDMetrics, Business Source License, method only, never imported). See:
docs/plans/2026-11-07-v2-4.4-copula-quality-report.md.

- Two numeric columns: `1 - |rho_real - rho_synth| / 2` with Spearman's rho, each on that frame's
  rows where both are present. Spearman rather than SDMetrics' default Pearson because a rank
  coefficient moves by O(1/n) for any one row, however extreme its value.
- Any pair with a categorical column: `1 - TVD` between the normalized joint frequency tables of
  the recoded discrete states (numeric columns binned by the model's own edges, null its own
  state, states under the floor merged; `_quality_inputs.discrete_states`).

`score_pairs` is the only builder of a `PairScore`, and it asks the gate first.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from decoy_engine.generation.copula import _floors
from decoy_engine.generation.copula._quality_gate import (
    METRIC_SPEARMAN,
    STATUS_SCORED,
    STATUS_UNDEFINED,
    UNDEFINED_CONSTANT,
    UNDEFINED_TOO_FEW,
    GateContext,
    metric_for,
    withhold_reason,
)
from decoy_engine.generation.copula._quality_inputs import IntArray


@dataclass(frozen=True)
class PairScore:
    columns: tuple[str, str]
    metric: str
    status: str
    reason: str | None
    score: float | None


def release(value: float) -> float:
    return round(float(value), _floors.RELEASE_DECIMALS) + 0.0


def spearman(x: pd.Series, y: pd.Series) -> float | None:
    """Spearman's rho on the rows where both are present: Pearson correlation of average-tie
    ranks of that subset. None when either side is constant or fewer than 2 rows remain."""
    both = (x.notna() & y.notna()).to_numpy()
    if int(both.sum()) < 2:
        return None
    rx = pd.Series(x.to_numpy(dtype=np.float64)[both]).rank(method="average").to_numpy()
    ry = pd.Series(y.to_numpy(dtype=np.float64)[both]).rank(method="average").to_numpy()
    dx, dy = rx - rx.mean(), ry - ry.mean()
    denominator = float(np.sqrt((dx * dx).sum() * (dy * dy).sum()))
    if denominator == 0.0:
        return None
    return float(np.clip((dx * dy).sum() / denominator, -1.0, 1.0))


def contingency(real_a: IntArray, real_b: IntArray, synth_a: IntArray, synth_b: IntArray) -> float:
    """1 - total variation distance between the two normalized joint tables."""
    width = int(max(real_b.max(initial=0), synth_b.max(initial=0))) + 1
    size = (int(max(real_a.max(initial=0), synth_a.max(initial=0))) + 1) * width
    p = np.bincount(real_a * width + real_b, minlength=size) / real_a.size
    q = np.bincount(synth_a * width + synth_b, minlength=size) / synth_a.size
    return 1.0 - 0.5 * float(np.abs(p - q).sum())


def score_pairs(
    ctx: GateContext, synthetic: pd.DataFrame, synthetic_states: dict[str, IntArray]
) -> tuple[PairScore, ...]:
    """Every unordered pair of model columns, in model order."""
    columns = ctx.model.columns
    out: list[PairScore] = []
    for a in range(len(columns)):
        for b in range(a + 1, len(columns)):
            metric = metric_for(ctx.model, a, b)
            names = (columns[a].name, columns[b].name)
            gate = withhold_reason(ctx, a, b, metric)
            if gate is not None:
                out.append(PairScore(names, metric, gate[0], gate[1], None))
                continue
            if metric == METRIC_SPEARMAN:
                score = _spearman_score(ctx.real, synthetic, names)
                if isinstance(score, str):
                    out.append(PairScore(names, metric, STATUS_UNDEFINED, score, None))
                    continue
            else:
                score = contingency(
                    ctx.states[names[0]],
                    ctx.states[names[1]],
                    synthetic_states[names[0]],
                    synthetic_states[names[1]],
                )
            out.append(PairScore(names, metric, STATUS_SCORED, None, release(score)))
    return tuple(out)


def _spearman_score(
    real: pd.DataFrame, synthetic: pd.DataFrame, names: tuple[str, str]
) -> float | str:
    synth_x, synth_y = synthetic[names[0]], synthetic[names[1]]
    if int((synth_x.notna() & synth_y.notna()).sum()) < 2:
        return UNDEFINED_TOO_FEW
    rho_synth = spearman(synth_x, synth_y)
    if rho_synth is None:
        return UNDEFINED_CONSTANT
    rho_real = spearman(real[names[0]], real[names[1]])
    if rho_real is None:
        # Unreachable behind the gate (it requires a non-constant real side on enough rows).
        raise RuntimeError("copula quality report: real pair passed the gate while constant.")
    return 1.0 - abs(rho_real - rho_synth) / 2.0
