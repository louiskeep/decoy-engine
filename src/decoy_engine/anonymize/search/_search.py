"""Least-loss lattice node meeting privacy targets within a record-suppression limit.

Pattern: optimal full-domain generalization with record suppression and lower-bound pruning (Incognito, LeFevre et al. 2005; Flash, Kohlmayer et al. 2012; ARX).
  Nodes are visited bottom-up by height (Incognito's order). A node's
  classes come from rolling up the base frequency set, never from
  regrouping rows. Outliers are the rows of classes failing any model; a
  node is feasible when they fit the suppression limit (ARX's semantics:
  the suppressed group is not itself checked, and t compares with the whole
  input). See: K. LeFevre et al., "Incognito", SIGMOD 2005; F. Kohlmayer et
  al., "Flash", PASSAT 2012; https://github.com/arx-deidentifier/arx.

Why the result is optimal. For each metric the value with nothing
suppressed is a lower bound of the value at the same node and never
decreases from a node to a successor. So a node whose bound exceeds the best
value found rules out every node above it, and a feasible node with nothing
suppressed rules out every node above it too (none can have a lower value,
and the tie-break prefers fewer levels). Nothing else is pruned. No
monotonicity of the privacy models is assumed, so entropy l, recursive
(c, l) and t-closeness, which are not monotone under suppression, are
handled exactly. Predictive tagging of the privacy property (Flash) is not
used: the cost of a node is its rollup, which the bound needs anyway.

Ties are broken by fewer suppressed rows, then the smaller sum of levels,
then the lexicographically smaller node. Levels must be nested on the data
(each class at one level inside one class at the next); the builders
guarantee it and the search checks it, since ``Hierarchy`` is open to
subclasses.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.anonymize.hierarchies import Lattice
from decoy_engine.anonymize.search._loss import (
    ColumnCosts,
    IntArray,
    Losses,
    LossMetric,
    Metric,
    column_costs,
    lower_bound,
    value,
)
from decoy_engine.anonymize.search._result import BUILT_IN_MODELS, SearchResult
from decoy_engine.quality.anonymity import FrequencySet, PrivacyModel

MAX_LATTICE_NODES = 5_000

Node = tuple[int, ...]


def check_arguments(
    table: object, lattice: object, models: object, metric: object, suppression_limit: object
) -> tuple[Lattice, tuple[PrivacyModel, ...], Metric, float]:
    """Type checks, then the lattice ceiling; nothing is read from the table."""
    if not isinstance(table, (pd.DataFrame, pa.Table)):
        raise TypeError("table must be a pandas.DataFrame or a pyarrow.Table")
    if not isinstance(lattice, Lattice):
        raise TypeError("lattice must be an anonymize.hierarchies.Lattice")
    if isinstance(models, (str, bytes)) or not isinstance(models, Sequence):
        raise TypeError("models must be a sequence of privacy models")
    checked = tuple(models)
    if not checked:
        raise ValueError("models must name at least one privacy model")
    if not all(type(m) in BUILT_IN_MODELS for m in checked):
        raise TypeError(
            "models must be the built-in privacy models from decoy_engine.quality.anonymity "
            "(subclasses are refused)"
        )
    if not isinstance(metric, Metric):
        raise TypeError("metric must be Discernibility(), LossMetric() or NonUniformEntropy()")
    if type(suppression_limit) not in (int, float):
        raise TypeError("suppression_limit must be an int or float (not bool or str)")
    limit = float(suppression_limit)  # type: ignore[arg-type]
    if not (math.isfinite(limit) and 0.0 <= limit <= 1.0):
        raise ValueError("suppression_limit must be between 0 and 1")
    if lattice.size > MAX_LATTICE_NODES:
        raise ValueError(
            f"the lattice has {lattice.size} nodes, above the {MAX_LATTICE_NODES}-node "
            "ceiling for an optimal search; use fewer columns or levels"
        )
    return lattice, checked, metric, limit


def allowed_rows(limit: float, rows: int) -> int:
    """Rows the limit allows, reading the limit as the decimal it prints as."""
    # Fraction(0.29) is the binary value, just under 29/100; repr gives 29/100.
    return math.floor(Fraction(repr(limit)) * rows)


@dataclass(frozen=True)
class _Prepared:
    base: FrequencySet
    columns: tuple[ColumnCosts, ...]


def _representative_table(
    table: pd.DataFrame | pa.Table, columns: tuple[str, ...], positions: IntArray
) -> pd.DataFrame | pa.Table:
    if isinstance(table, pd.DataFrame):
        return table.loc[:, list(columns)].iloc[positions]
    return table.select(list(columns)).take(pa.array(positions, type=pa.int64()))


def _column(table: pd.DataFrame | pa.Table, name: str) -> pd.Series | pa.ChunkedArray:
    if isinstance(table, pd.DataFrame):
        return table[name]
    return table.column(name)


def _one_column(name: str, labels: object) -> pd.DataFrame | pa.Table:
    if isinstance(labels, pd.Series):
        return pd.DataFrame({name: labels.reset_index(drop=True)})
    return pa.table({name: labels})


def _level_codes(lattice: Lattice, reps: pd.DataFrame | pa.Table, name: str) -> list[IntArray]:
    hierarchy = lattice.hierarchies[name]
    out = []
    for level in range(hierarchy.height + 1):
        error: tuple[type[Exception], str] | None = None
        try:
            labels = hierarchy.generalize(_column(reps, name), level)
        except (TypeError, ValueError) as exc:
            # Value-free by the hierarchy contract; the type is kept, the chain is not.
            error = (TypeError if isinstance(exc, TypeError) else ValueError, str(exc))
        if error is not None:
            raise error[0](f"column {name!r}: {error[1]}")
        out.append(FrequencySet.of(_one_column(name, labels), (name,)).row_class)
    return out


def check_nested(name: str, level_codes: list[IntArray]) -> None:
    """Each class at a level must sit inside one class at the next level."""
    for level in range(len(level_codes) - 1):
        finer, coarser = level_codes[level], level_codes[level + 1]
        if not finer.size:
            continue
        up = np.full(int(finer.max()) + 1, -1, dtype=np.int64)
        up[finer] = coarser
        if not bool(np.array_equal(up[finer], coarser)):
            raise ValueError(
                f"column {name!r}: hierarchy level {level + 1} splits a class of level "
                f"{level}; the search needs nested levels"
            )


def _prepare(
    table: pd.DataFrame | pa.Table, lattice: Lattice, models: tuple[PrivacyModel, ...]
) -> _Prepared:
    base = FrequencySet.of(table, lattice.columns, models)
    # Coverage and time-zone checks on every cell, so row counts are reported
    # and the search refuses exactly what apply_search would refuse.
    lattice.apply(table, lattice.bottom)
    reps = _representative_table(table, lattice.columns, base.representatives)
    costs = []
    for name in lattice.columns:
        codes = _level_codes(lattice, reps, name)
        check_nested(name, codes)
        costs.append(column_costs(codes, base.sizes, base.rows))
    return _Prepared(base=base, columns=tuple(costs))


@dataclass
class _Best:
    node: Node
    key: tuple[object, ...]
    value: Losses
    rows_suppressed: int
    class_count: int
    k_min: int | None


def search_lattice(
    table: pd.DataFrame | pa.Table,
    lattice: Lattice,
    models: Sequence[PrivacyModel],
    *,
    suppression_limit: float = 0.0,
    metric: Metric | None = None,
) -> SearchResult:
    """The least-loss node of ``lattice`` meeting every model within the limit.

    Never modifies ``table``. Returns a result with ``found=False`` when no
    node is feasible. See the module docstring for the guarantee.
    """
    lattice, checked, objective, limit = check_arguments(
        table, lattice, models, LossMetric() if metric is None else metric, suppression_limit
    )
    prepared = _prepare(table, lattice, checked)
    base = prepared.base
    rows = base.rows
    allowed = allowed_rows(limit, rows)
    best: _Best | None = None
    dominated: set[Node] = set()
    evaluated = 0
    for node in sorted(lattice.nodes(), key=lambda n: (sum(n), n)):
        if any(p in dominated for p in lattice.predecessors(node)):
            dominated.add(node)
            continue
        evaluated += 1
        node_set = base.rollup(
            [c.levels[h].codes for c, h in zip(prepared.columns, node, strict=True)]
        )
        failing = node_set.failing(checked)
        bound = lower_bound(prepared.columns, node, node_set.sizes)
        if best is not None and objective.exceeds(
            objective.pick(bound), objective.pick(best.value)
        ):
            dominated.add(node)
            continue
        suppressed = int(node_set.sizes[failing].sum())
        if suppressed > allowed:
            continue
        found = value(
            prepared.columns,
            node,
            base.sizes,
            node_set.sizes,
            ~failing,
            failing[node_set.base_class],
        )
        key = (objective.pick(found), suppressed, sum(node), node)
        if best is None or key < best.key:
            kept = node_set.sizes[~failing]
            best = _Best(
                node=node,
                key=key,
                value=found,
                rows_suppressed=suppressed,
                class_count=int(kept.shape[0]),
                k_min=int(kept.min()) if kept.size else None,
            )
        if suppressed == 0:
            dominated.add(node)
    return _result(lattice, checked, objective, limit, rows, allowed, evaluated, best)


def _result(
    lattice: Lattice,
    models: tuple[PrivacyModel, ...],
    metric: Metric,
    limit: float,
    rows: int,
    allowed: int,
    evaluated: int,
    best: _Best | None,
) -> SearchResult:
    common = {
        "columns": lattice.columns,
        "heights": lattice.heights,
        "models": models,
        "metric": metric.name,
        "lattice_size": lattice.size,
        "nodes_evaluated": evaluated,
        "rows": rows,
        "suppression_limit": limit,
        "allowed_suppressed": allowed,
    }
    if best is None:
        return SearchResult(
            node=None,
            found=False,
            rows_suppressed=None,
            loss=None,
            discernibility=None,
            loss_metric=None,
            non_uniform_entropy=None,
            class_count=None,
            k_min=None,
            **common,  # type: ignore[arg-type]
        )
    return SearchResult(
        node=best.node,
        found=True,
        rows_suppressed=best.rows_suppressed,
        loss=float(metric.pick(best.value)),
        discernibility=best.value.discernibility,
        loss_metric=float(best.value.loss_metric),
        non_uniform_entropy=float(best.value.non_uniform_entropy),
        class_count=best.class_count,
        k_min=best.k_min,
        **common,  # type: ignore[arg-type]
    )
