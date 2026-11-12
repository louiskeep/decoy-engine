"""An independent brute-force reference for the search, written without the code under test.

Classes are grouped in plain Python over the labels ``Lattice.apply`` gives;
k and l verdicts are counted directly; t uses a ``Fraction`` EMD against the
whole table's sensitive distribution (not ``measure_t_closeness_*``, which on
a one-class table would compare the class with itself); losses use
``Fraction`` and per-row ``math.log2``.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import pandas as pd

from decoy_engine.anonymize.hierarchies import Lattice
from decoy_engine.quality.anonymity import (
    DistinctLDiversity,
    EntropyLDiversity,
    EqualTCloseness,
    KAnonymity,
    OrderedTCloseness,
    PrivacyModel,
    RecursiveCLDiversity,
)

NULL = ("null",)


def key(value: Any) -> Any:
    """Value identity for the simple test columns: type family plus value, nulls one."""
    if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
        return NULL
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    return (type(value).__name__, value)


def _nonnull(values: list[Any]) -> list[Any]:
    return [v for v in values if key(v) != NULL]


def _equal_emd(cls: list[Any], table: list[Any]) -> Fraction:
    p, q = Counter(map(key, cls)), Counter(map(key, table))
    total = sum(abs(Fraction(p[v], len(cls)) - Fraction(q[v], len(table))) for v in set(p) | set(q))
    return Fraction(total) / 2


def _ordered_emd(cls: list[Any], table: list[Any]) -> Fraction:
    order = sorted(set(table))
    m = len(order)
    if m <= 1:
        return Fraction(0)
    p, q = Counter(cls), Counter(table)
    running, total = Fraction(0), Fraction(0)
    for value in order[:-1]:
        running += Fraction(p[value], len(cls)) - Fraction(q[value], len(table))
        total += abs(running)
    return total / (m - 1)


def fails(model: PrivacyModel, rows: list[int], frame: pd.DataFrame) -> bool:
    if isinstance(model, KAnonymity):
        return len(rows) < model.k
    column = frame[model.sensitive].tolist()  # type: ignore[attr-defined]
    values = _nonnull([column[r] for r in rows])
    if isinstance(model, DistinctLDiversity):
        return len(set(map(key, values))) < model.l
    if not values:
        return True
    counts = sorted(Counter(map(key, values)).values(), reverse=True)
    if isinstance(model, EntropyLDiversity):
        h = -sum((c / len(values)) * math.log(c / len(values)) for c in counts)
        return h < math.log(model.l) - 1e-12
    if isinstance(model, RecursiveCLDiversity):
        best = 0
        for level in range(1, len(counts) + 1):
            if counts[0] < model.c * sum(counts[level - 1 :]):
                best = level
        return best < model.l
    table = _nonnull(column)
    if isinstance(model, EqualTCloseness):
        return float(_equal_emd(values, table)) > model.t + 1e-12
    if isinstance(model, OrderedTCloseness):
        return float(_ordered_emd(values, table)) > model.t + 1e-12
    raise AssertionError(type(model))


@dataclass(frozen=True)
class NodeOutcome:
    node: tuple[int, ...]
    suppressed: int
    feasible: bool
    discernibility: int
    loss_metric: Fraction
    non_uniform_entropy: float
    outliers: frozenset[int]


def evaluate(
    frame: pd.DataFrame, lattice: Lattice, models: tuple[PrivacyModel, ...], limit: float
) -> list[NodeOutcome]:
    n = len(frame)
    allowed = math.floor(Fraction(repr(float(limit))) * n)
    originals = {c: [key(v) for v in frame[c].tolist()] for c in lattice.columns}
    domain = {c: len(set(originals[c])) for c in lattice.columns}
    value_rows = {c: Counter(originals[c]) for c in lattice.columns}
    out = []
    for node in lattice.nodes():
        applied = lattice.apply(frame, node)
        labels = {c: [key(v) for v in applied[c].tolist()] for c in lattice.columns}
        groups: dict[tuple[Any, ...], list[int]] = {}
        for r in range(n):
            groups.setdefault(tuple(labels[c][r] for c in lattice.columns), []).append(r)
        outliers: set[int] = set()
        kept_square = 0
        for rows in groups.values():
            if any(fails(m, rows, frame) for m in models):
                outliers.update(rows)
            else:
                kept_square += len(rows) ** 2
        lm = Fraction(0)
        nue_terms = []
        for c in lattice.columns:
            under: dict[Any, set[Any]] = {}
            label_rows: Counter[Any] = Counter()
            for r in range(n):
                under.setdefault(labels[c][r], set()).add(originals[c][r])
                label_rows[labels[c][r]] += 1
            for r in range(n):
                v = originals[c][r]
                if r in outliers:
                    if domain[c] > 1:
                        lm += 1
                    nue_terms.append(math.log2(n / value_rows[c][v]))
                else:
                    if domain[c] > 1:
                        lm += Fraction(len(under[labels[c][r]]) - 1, domain[c] - 1)
                    nue_terms.append(math.log2(label_rows[labels[c][r]] / value_rows[c][v]))
        if n:
            lm /= n * len(lattice.columns)
        out.append(
            NodeOutcome(
                node=node,
                suppressed=len(outliers),
                feasible=len(outliers) <= allowed,
                discernibility=kept_square + n * len(outliers),
                loss_metric=lm,
                non_uniform_entropy=math.fsum(nue_terms),
                outliers=frozenset(outliers),
            )
        )
    return out


def loss_of(outcome: NodeOutcome, metric: str) -> Any:
    return {
        "discernibility": outcome.discernibility,
        "loss": outcome.loss_metric,
        "non_uniform_entropy": outcome.non_uniform_entropy,
    }[metric]


def best(outcomes: list[NodeOutcome], metric: str) -> NodeOutcome | None:
    feasible = [o for o in outcomes if o.feasible]
    if not feasible:
        return None
    return min(feasible, key=lambda o: (loss_of(o, metric), o.suppressed, sum(o.node), o.node))
