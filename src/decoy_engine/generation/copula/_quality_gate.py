"""The release gate for pair scores: every pair passes `withhold_reason` before any statistic of
the real frame is computed for it.

A pair score is a statistic of the real data: next to the synthetic table it gives the real
coefficient up to sign, so it obeys the same disclosure floors as the model's correlation
(`_floors.py`). The gate withholds a score when the model suppressed a latent variable it depends
on (otherwise the score would release exactly what the floors withheld), when the release budget
is spent, when a column is held with its full vocabulary, and when the rows the score uses fail
the floors on the frame passed in (the tie to the model checks marginals, not pair overlaps).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from decoy_engine.generation.copula import _floors
from decoy_engine.generation.copula._model import (
    KIND_EMPTY,
    KIND_NUMERIC,
    ROLE_VALUE,
    GaussianCopulaModel,
)
from decoy_engine.generation.copula._quality_inputs import IntArray, distinguished, value_groups

METRIC_SPEARMAN = "spearman_correlation_similarity"
METRIC_CONTINGENCY = "contingency_similarity"

STATUS_SCORED = "scored"
STATUS_WITHHELD = "withheld"
STATUS_UNDEFINED = "undefined"

WITHHELD_SUPPRESSED = "suppressed_in_model"
WITHHELD_PAIR_ROWS = "pair_rows"
WITHHELD_DISTINGUISHED = "distinguished_rows"
WITHHELD_HIGH_CARDINALITY = "high_cardinality"
WITHHELD_BUDGET = "release_budget"
UNDEFINED_EMPTY = "empty_column"
UNDEFINED_CONSTANT = "constant_in_synthetic"
UNDEFINED_TOO_FEW = "too_few_synthetic_rows"

WITHHELD_REASONS = frozenset(
    {
        WITHHELD_SUPPRESSED,
        WITHHELD_PAIR_ROWS,
        WITHHELD_DISTINGUISHED,
        WITHHELD_HIGH_CARDINALITY,
        WITHHELD_BUDGET,
    }
)
UNDEFINED_REASONS = frozenset({UNDEFINED_EMPTY, UNDEFINED_CONSTANT, UNDEFINED_TOO_FEW})


@dataclass(frozen=True)
class GateContext:
    """What the gate reads: the model's facts and the real frame's recoded states."""

    model: GaussianCopulaModel
    real: pd.DataFrame  # canonical
    states: dict[str, IntArray]  # recoded real states, for every non-empty column
    high_cardinality: frozenset[str]
    over_budget: bool


def over_budget(model: GaussianCopulaModel) -> bool:
    """The model's latent pairs plus every candidate column pair, against the copula's budget."""
    d = len(model.latent)
    c = sum(1 for column in model.columns if column.kind != KIND_EMPTY)
    return d * (d - 1) // 2 + c * (c - 1) // 2 > _floors.max_latent_pairs(model.row_count)


def metric_for(model: GaussianCopulaModel, a: int, b: int) -> str:
    numeric = model.columns[a].kind == KIND_NUMERIC and model.columns[b].kind == KIND_NUMERIC
    return METRIC_SPEARMAN if numeric else METRIC_CONTINGENCY


def _dependencies(model: GaussianCopulaModel, a: int, b: int, metric: str) -> set[int]:
    """Latent variables a score depends on: Spearman on the two value variables, contingency on
    every variable of both columns (a numeric column's null indicator included)."""
    out: set[int] = set()
    for index, variable in enumerate(model.latent):
        if variable.column not in (a, b):
            continue
        if metric == METRIC_CONTINGENCY or variable.role == ROLE_VALUE:
            out.add(index)
    return out


def withhold_reason(ctx: GateContext, a: int, b: int, metric: str) -> tuple[str, str] | None:
    """(status, reason) when the pair must not be scored, else None."""
    model = ctx.model
    first, second = model.columns[a], model.columns[b]
    if KIND_EMPTY in (first.kind, second.kind):
        return STATUS_UNDEFINED, UNDEFINED_EMPTY
    if ctx.over_budget:
        return STATUS_WITHHELD, WITHHELD_BUDGET
    suppressed = {s.variable for s in model.suppressed}
    if _dependencies(model, a, b, metric) & suppressed:
        return STATUS_WITHHELD, WITHHELD_SUPPRESSED
    if metric == METRIC_SPEARMAN:
        return _spearman_floors(ctx.real[first.name], ctx.real[second.name])
    if first.name in ctx.high_cardinality or second.name in ctx.high_cardinality:
        return STATUS_WITHHELD, WITHHELD_HIGH_CARDINALITY
    for name in (first.name, second.name):
        if distinguished(ctx.states[name]) < _floors.MIN_DISTINGUISHED_ROWS:
            return STATUS_WITHHELD, WITHHELD_DISTINGUISHED
    return None


def _spearman_floors(x: pd.Series, y: pd.Series) -> tuple[str, str] | None:
    """The copula's pair floors on the rows the coefficient uses."""
    both = (x.notna() & y.notna()).to_numpy()
    if int(both.sum()) < _floors.MIN_PAIR_ROWS:
        return STATUS_WITHHELD, WITHHELD_PAIR_ROWS
    for series in (x, y):
        groups = value_groups(series.to_numpy(dtype=np.float64)[both])
        if distinguished(groups) < _floors.MIN_DISTINGUISHED_ROWS:
            return STATUS_WITHHELD, WITHHELD_DISTINGUISHED
    return None
