"""Information loss of a full-domain generalization with record suppression.

Pattern: discernibility, loss metric and non-uniform entropy with ARX suppression penalties (Bayardo and Agrawal 2005; Iyengar 2002; Gionis and Tassa 2009).
  - Discernibility: ``sum |E|^2`` over classes kept plus ``rows`` for each
    suppressed row (ARX MetricSDNMDiscernability).
  - Loss metric: per column and row the share ``(M - 1) / (D - 1)`` of the
    column's observed distinct values ``D`` that the row's label covers
    (``M`` of them); a suppressed row has share 1; averaged over columns and
    rows (Iyengar 2002; ARX MetricMDNMLoss with its default 0.5
    generalization/suppression factor).
  - Non-uniform entropy: ``sum -log2(c(v) / c(label))`` over rows and
    columns, counts over all input rows; a suppressed row's label covers all
    rows (Gionis and Tassa, IEEE TKDE 21(2), 2009).
  Each metric's value with no row suppressed is a lower bound of its value
  at the same node and never decreases from a node to a successor (classes
  only merge and labels only coarsen), which is what makes bound pruning
  optimal (ARX's lower bounds). See: https://github.com/arx-deidentifier/arx.

``D`` and ``M`` count observed values (null is one value), not a declared
domain. A column with one observed value loses nothing. Discernibility and
the loss metric are exact (``int`` and ``fractions.Fraction``); non-uniform
entropy is a float summed with ``math.fsum``, which is exactly rounded and so
independent of summation order.
"""

from __future__ import annotations

import abc
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import ClassVar

import numpy as np
import numpy.typing as npt

IntArray = npt.NDArray[np.int64]
FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]
Exact = int | Fraction | float

# Pruning margin for the float metric: a bound must clear the best by more
# than rounding before a node is ruled out.
_FLOAT_MARGIN = 1e-9


@dataclass(frozen=True)
class LevelCosts:
    """Per base class costs of one column at one level."""

    codes: IntArray
    label_rows: IntArray
    share: IntArray
    share_total: int
    entropy: FloatArray
    entropy_total: float


@dataclass(frozen=True)
class ColumnCosts:
    """One column's costs at every level."""

    domain: int
    levels: tuple[LevelCosts, ...]


def _summed(index: IntArray, weights: IntArray, length: int) -> IntArray:
    out = np.zeros(length, dtype=np.int64)
    np.add.at(out, index, weights)
    return out


def column_costs(level_codes: list[IntArray], sizes: IntArray, rows: int) -> ColumnCosts:
    """Costs from the per-level codes of one column over the base classes.

    Relies on nested levels (checked before): each level-0 code maps to one
    label code, so the labels' value counts follow from a scatter.
    """
    base0 = level_codes[0]
    domain = int(base0.max()) + 1 if base0.size else 0
    value_rows = _summed(base0, sizes, domain)
    levels = []
    for codes in level_codes:
        labels = int(codes.max()) + 1 if codes.size else 0
        label_of_value = np.zeros(domain, dtype=np.int64)
        label_of_value[base0] = codes
        values_under = np.bincount(label_of_value, minlength=labels).astype(np.int64)
        share = values_under[codes] - 1 if codes.size else np.zeros(0, dtype=np.int64)
        label_rows = _summed(codes, sizes, labels)
        entropy = (
            np.log2(label_rows[codes] / value_rows[base0])
            if codes.size
            else np.zeros(0, dtype=np.float64)
        )
        levels.append(
            LevelCosts(
                codes=codes,
                label_rows=label_rows,
                share=share.astype(np.int64),
                share_total=int(np.dot(sizes, share)),
                entropy=entropy,
                entropy_total=math.fsum((sizes * entropy).tolist()),
            )
        )
    return ColumnCosts(domain=domain, levels=tuple(levels))


@dataclass(frozen=True)
class Losses:
    """The three metrics at one node, exact where they can be."""

    discernibility: int
    loss_metric: Fraction
    non_uniform_entropy: float


def lower_bound(
    columns: tuple[ColumnCosts, ...], node: tuple[int, ...], class_sizes: IntArray
) -> Losses:
    """Each metric as if nothing were suppressed: below the value, and never
    lower at a successor."""
    rows = int(class_sizes.sum())
    lm = Fraction(0)
    for column, level in zip(columns, node, strict=True):
        if column.domain > 1:
            lm += Fraction(column.levels[level].share_total, column.domain - 1)
    if rows:
        lm /= rows * len(columns)
    entropy = math.fsum(
        column.levels[level].entropy_total for column, level in zip(columns, node, strict=True)
    )
    return Losses(int((class_sizes * class_sizes).sum()), lm, entropy)


def value(
    columns: tuple[ColumnCosts, ...],
    node: tuple[int, ...],
    base_sizes: IntArray,
    class_sizes: IntArray,
    kept_classes: BoolArray,
    suppressed_base: BoolArray,
) -> Losses:
    """Each metric at ``node`` with the flagged base classes suppressed."""
    rows = int(base_sizes.sum())
    sup_sizes = base_sizes[suppressed_base]
    suppressed_rows = int(sup_sizes.sum())
    kept = class_sizes[kept_classes]
    dm = int((kept * kept).sum()) + rows * suppressed_rows
    lm = Fraction(0)
    entropy_parts: list[float] = []
    for column, level in zip(columns, node, strict=True):
        costs = column.levels[level]
        entropy_parts.append(costs.entropy_total)
        if suppressed_rows:
            # A suppressed row's term rises from log2(c(label)/c(v)) to
            # log2(rows/c(v)): by log2(rows/c(label)), one figure per label.
            per_label = _summed(costs.codes[suppressed_base], sup_sizes, costs.label_rows.shape[0])
            hit = np.flatnonzero(per_label)
            entropy_parts.append(
                math.fsum((per_label[hit] * np.log2(rows / costs.label_rows[hit])).tolist())
            )
        if column.domain <= 1:
            continue
        numerator = (
            costs.share_total
            - int(np.dot(sup_sizes, costs.share[suppressed_base]))
            + suppressed_rows * (column.domain - 1)
        )
        lm += Fraction(numerator, column.domain - 1)
    if rows:
        lm /= rows * len(columns)
    return Losses(dm, lm, math.fsum(entropy_parts))


def losses(
    columns: tuple[ColumnCosts, ...],
    node: tuple[int, ...],
    base_sizes: IntArray,
    class_sizes: IntArray,
    kept_classes: BoolArray,
    suppressed_base: BoolArray,
) -> tuple[Losses, Losses]:
    """(value, lower bound) at ``node``."""
    return (
        value(columns, node, base_sizes, class_sizes, kept_classes, suppressed_base),
        lower_bound(columns, node, class_sizes),
    )


class Metric(abc.ABC):
    """The objective of the search; all three metrics are always reported."""

    __slots__ = ()

    name: ClassVar[str]

    @abc.abstractmethod
    def pick(self, value: Losses) -> Exact:
        """This metric's value."""

    def exceeds(self, bound: Exact, best: Exact) -> bool:
        """Whether a bound rules a node (and its successors) out against ``best``."""
        return bound > best

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self)

    def __hash__(self) -> int:
        return hash(type(self))

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class Discernibility(Metric):
    """Sum of squared class sizes, suppressed rows costing the row count each."""

    __slots__ = ()
    name: ClassVar[str] = "discernibility"

    def pick(self, value: Losses) -> Exact:
        return value.discernibility


class LossMetric(Metric):
    """Iyengar's loss metric, 0 (nothing lost) to 1 (everything suppressed)."""

    __slots__ = ()
    name: ClassVar[str] = "loss"

    def pick(self, value: Losses) -> Exact:
        return value.loss_metric


class NonUniformEntropy(Metric):
    """Gionis and Tassa's non-uniform entropy, in bits."""

    __slots__ = ()
    name: ClassVar[str] = "non_uniform_entropy"

    def pick(self, value: Losses) -> Exact:
        return value.non_uniform_entropy

    def exceeds(self, bound: Exact, best: Exact) -> bool:
        return float(bound) > float(best) + _FLOAT_MARGIN * max(1.0, abs(float(best)))
