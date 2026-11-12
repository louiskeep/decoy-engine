"""Re-identification risk of a table under the prosecutor, journalist and marketer models.

Pattern: ARX sample risk summary (El Emam's prosecutor, journalist and marketer risk).
  Every figure is a function of equivalence-class sizes: ``f`` in the table and,
  for the journalist and marketer models, ``F``, the size of the same class in a
  population the table was drawn from. ARX ``RiskModelSampleSummary``
  (https://github.com/arx-deidentifier/arx, Apache-2.0) implements "risk measures
  as proposed by El Emam": K. El Emam and F. K. Dankar, "Protecting privacy using
  k-anonymity", JAMIA 15(5) 2008; F. K. Dankar and K. El Emam, "A method for
  evaluating marketer re-identification risk", EDBT/ICDT workshops 2010; K. El
  Emam, "Guide to the De-Identification of Personal Health Information", 2013.

For ``n`` rows in ``J`` classes and a threshold ``tau``:

- prosecutor (the attacker knows the target is in the table): highest
  ``1 / min f``, average ``J / n``, records at risk ``sum{f : 1/f > tau} / n``;
- journalist (the table is a sample of a known population): highest
  ``1 / min F``, average ``max(J / sum F, sum(f / F) / n)``, records at risk
  ``sum{f : 1/F > tau} / n``, where the sums run over the table's classes only;
- marketer: ``sum(f / F) / n``.

Departures from ARX, each deliberate:

- Without a population no journalist or marketer figure is given. ARX then
  uses the table as its own population, which writes the prosecutor figures
  under the journalist name.
- ``tau`` is read as the decimal it prints as (``Fraction(repr(tau))``), so the
  comparison ``1/f > tau`` is exact; ARX allows up to 1% rounding slack.
- Sums of ratios use ``math.fsum`` (exactly rounded, so the order of classes
  does not matter).
- An empty table gives ``None``, not ARX's 0, which would read as "no risk".

The table and the population are grouped together through the one class
identity of this package. They must hold the same value types per column
(``1`` and ``1.0`` are two values); a column whose table holds a type the
population column does not is refused rather than reported as a missing class.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.anonymize import type_family
from decoy_engine.quality.anonymity._classes import (
    IntArray,
    _real,
    check_columns,
    equivalence_classes,
    null_mask,
    read_columns,
)
from decoy_engine.quality.anonymity._report import check_report_values


def prosecutor_highest(smallest_class: int) -> float:
    """Highest prosecutor risk: one over the smallest class."""
    return 1.0 / smallest_class


def prosecutor_average(classes: int, rows: int) -> float:
    """Average prosecutor risk: classes over rows."""
    return classes / rows


@dataclass(frozen=True, slots=True)
class ReidentificationRiskReport:
    """Risk figures; ``None`` where the table is empty or no population was given."""

    row_count: int
    class_count: int
    k_min: int | None
    risk_threshold: float
    rows_at_risk_prosecutor: int | None
    records_at_risk_prosecutor: float | None
    highest_prosecutor_risk: float | None
    average_prosecutor_risk: float | None
    population_rows: int | None
    rows_at_risk_journalist: int | None
    records_at_risk_journalist: float | None
    highest_journalist_risk: float | None
    average_journalist_risk: float | None
    average_marketer_risk: float | None

    def __post_init__(self) -> None:
        check_report_values(self)


def check_risk_threshold(value: object) -> float:
    """``risk_threshold`` is a finite real in ``(0, 1]`` (not ``bool``, not a string)."""
    number = _real("risk_threshold", value)
    if not 0.0 < number <= 1.0:
        raise ValueError("risk_threshold must be greater than 0 and at most 1")
    return number


def _at_risk_rows(sizes: IntArray, weights: IntArray, threshold: Fraction) -> int:
    """Rows (``weights``) of the classes whose ``1 / size`` exceeds ``threshold``."""
    at_risk = 0
    for size in np.unique(sizes):
        if int(size) * threshold.numerator < threshold.denominator:
            at_risk += int(weights[sizes == size].sum())
    return at_risk


def _families(series: pd.Series) -> set[str]:
    """Type families of the non-null cells (``type_family`` depends on the type only)."""
    values = series[~null_mask(series)].tolist()
    examples = dict(zip(map(type, values), values, strict=True))
    return {type_family(example) for example in examples.values()}


def _population_sizes(
    sample: pd.DataFrame, population: pd.DataFrame, columns: tuple[str, ...]
) -> tuple[IntArray, IntArray]:
    """Per class of ``sample``: its size and the size of the same class in ``population``."""
    for name in columns:
        # A column with no non-null population cell has no type to disagree with; its
        # classes are reported as missing instead.
        held = _families(population[name])
        if held and not _families(sample[name]) <= held:
            raise TypeError(
                f"column {name!r} holds value types in the table that the population column "
                "does not hold; the table and the population must have the same column types"
            )
    joint = pd.concat(
        [sample.astype(object), population.astype(object)], ignore_index=True, copy=False
    )
    ids = equivalence_classes(joint, columns).ids
    rows = len(sample)
    count = int(ids.max()) + 1 if ids.size else 0
    in_sample = np.bincount(ids[:rows], minlength=count).astype(np.int64)
    in_population = np.bincount(ids[rows:], minlength=count).astype(np.int64)
    released = in_sample > 0
    return in_sample[released], in_population[released]


def measure_reidentification_risk(
    table: pd.DataFrame | pa.Table,
    quasi_identifiers: Sequence[str],
    *,
    risk_threshold: float,
    population: pd.DataFrame | pa.Table | None = None,
) -> ReidentificationRiskReport:
    """Prosecutor risk of ``table`` over ``quasi_identifiers``; journalist and marketer risk too
    when ``population`` (a table containing ``table``'s rows, at the same generalization) is
    given. Raises ``ValueError`` when a class of ``table`` is missing from ``population`` or
    smaller in it."""
    columns = check_columns(table, quasi_identifiers, None)
    tau = check_risk_threshold(risk_threshold)
    if population is not None:
        check_columns(population, columns, None)
    threshold = Fraction(repr(tau))
    frame = read_columns(table, columns)
    sizes = equivalence_classes(frame, columns).sizes
    rows = int(sizes.sum())
    count = int(sizes.shape[0])

    prosecutor: tuple[int | None, float | None, float | None, float | None] = (None,) * 4
    if rows:
        at_risk = _at_risk_rows(sizes, sizes, threshold)
        prosecutor = (
            at_risk,
            at_risk / rows,
            prosecutor_highest(int(sizes.min())),
            prosecutor_average(count, rows),
        )

    population_rows: int | None = None
    journalist: tuple[int | None, float | None, float | None, float | None] = (None,) * 4
    marketer: float | None = None
    if population is not None:
        other = read_columns(population, columns)
        population_rows = len(other)
        sample_sizes, big = _population_sizes(frame, other, columns)
        short = int((big < sample_sizes).sum())
        if short:
            raise ValueError(
                f"the population is not a superset of the release: {short} released classes "
                "are missing from it or smaller in it"
            )
        if rows:
            ratio = math.fsum((sample_sizes / big).tolist()) / rows
            at_risk = _at_risk_rows(big, sample_sizes, threshold)
            journalist = (
                at_risk,
                at_risk / rows,
                1.0 / int(big.min()),
                max(count / int(big.sum()), ratio),
            )
            marketer = ratio

    return ReidentificationRiskReport(
        row_count=rows,
        class_count=count,
        k_min=int(sizes.min()) if count else None,
        risk_threshold=tau,
        rows_at_risk_prosecutor=prosecutor[0],
        records_at_risk_prosecutor=prosecutor[1],
        highest_prosecutor_risk=prosecutor[2],
        average_prosecutor_risk=prosecutor[3],
        population_rows=population_rows,
        rows_at_risk_journalist=journalist[0],
        records_at_risk_journalist=journalist[1],
        highest_journalist_risk=journalist[2],
        average_journalist_risk=journalist[3],
        average_marketer_risk=marketer,
    )
