"""Hand-computed search results on small tables.

Lattice (``small_lattice``): age at value / ``[20, 30)``-style decades / ``*``;
zip at value / two-character prefix (``"12*"``) / ``*``.
"""

from __future__ import annotations

import math
from fractions import Fraction

import pandas as pd
import pytest

from decoy_engine.anonymize.search import (
    Discernibility,
    LossMetric,
    NonUniformEntropy,
    search_lattice,
)
from decoy_engine.quality.anonymity import KAnonymity

from ._fixtures import seven_rows, six_rows, small_lattice

LOG3 = math.log2(3)


@pytest.mark.parametrize("metric", [Discernibility(), LossMetric(), NonUniformEntropy()])
def test_six_rows_k2_no_suppression(metric: object) -> None:
    """(1, 1) gives classes (20s, 12*) x 3 and (30s, 13*) x 3.

    Every lower node has a singleton class. (2, 1) and (1, 2) also pass but
    lose more: discernibility 18 for all three, broken by level sum.
    DM = 3^2 + 3^2 = 18.
    LM: age D = 6, each decade covers 3 values -> 2/5 per row, 12/5 in all;
    zip D = 4, each prefix covers 2 values -> 1/3 per row, 2 in all;
    (12/5 + 2) / (6 * 2) = 11/30.
    NUE: age 6 * log2(3/1); zip 4 * log2(3/2) + 2 * log2(3) -> 12 log2 3 - 4.
    Visited in (level sum, node) order: (0,0), (0,1), (1,0), (0,2), (1,1),
    (2,0); (1,1) is feasible with nothing suppressed, so (1,2), (2,1) and
    (2,2) above it are never evaluated.
    """
    result = search_lattice(six_rows(), small_lattice(), (KAnonymity(2),), metric=metric)  # type: ignore[arg-type]
    assert result.found
    assert result.node == (1, 1)
    assert result.rows_suppressed == 0
    assert result.discernibility == 18
    assert result.loss_metric == float(Fraction(11, 30))
    assert result.non_uniform_entropy == pytest.approx(12 * LOG3 - 4, abs=1e-12)
    assert result.class_count == 2
    assert result.k_min == 3
    assert result.nodes_evaluated == 6
    assert result.lattice_size == 9


def test_seven_rows_suppression_admits_a_lower_node() -> None:
    """The 55-year-old in 210 is alone at every node below the top.

    No suppression: only (2, 2) passes; DM = 7^2 = 49, LM = 1.
    suppression_limit 0.15 -> floor(0.15 * 7) = 1 row: (1, 1) suppresses it.
    DM = 9 + 9 + 7 * 1 = 25.
    LM: age D = 7, decades cover 3 values -> 2/6 each for 6 rows, 1 for the
    suppressed row: 3; zip D = 5, prefixes cover 2 -> 1/4 each for 6 rows,
    1 for the suppressed row: 5/2; (3 + 5/2) / (7 * 2) = 11/28.
    NUE: age 6 log2 3 + log2 7; zip 4 log2(3/2) + 2 log2 3 + log2 7.
    """
    plain = search_lattice(seven_rows(), small_lattice(), (KAnonymity(2),))
    assert plain.node == (2, 2)
    assert plain.rows_suppressed == 0
    assert plain.discernibility == 49
    assert plain.loss_metric == 1.0

    for metric in (Discernibility(), LossMetric(), NonUniformEntropy()):
        result = search_lattice(
            seven_rows(), small_lattice(), (KAnonymity(2),), suppression_limit=0.15, metric=metric
        )
        assert result.node == (1, 1)
        assert result.rows_suppressed == 1
        assert result.allowed_suppressed == 1
        assert result.discernibility == 25
        assert result.loss_metric == float(Fraction(11, 28))
        assert result.non_uniform_entropy == pytest.approx(
            12 * LOG3 - 4 + 2 * math.log2(7), abs=1e-12
        )
        assert result.class_count == 2
        assert result.k_min == 3


@pytest.mark.parametrize(("limit", "rows", "allowed"), [(0.29, 100, 29), (0.3, 10, 3)])
def test_budget_reads_the_printed_decimal(limit: float, rows: int, allowed: int) -> None:
    """floor(Fraction(0.29) * 100) is 28 and floor(Fraction(0.3) * 10) is 2:
    the binary values sit just under the decimals. The limit means the decimal."""
    assert math.floor(Fraction(limit) * rows) == allowed - 1
    table = pd.DataFrame({"age": [30] * rows, "zip": ["120"] * rows})
    result = search_lattice(table, small_lattice(), (KAnonymity(1),), suppression_limit=limit)
    assert result.allowed_suppressed == allowed


def test_no_feasible_node() -> None:
    table = six_rows()
    result = search_lattice(table, small_lattice(), (KAnonymity(7),))
    assert not result.found
    assert result.node is None
    assert result.rows_suppressed is None
    assert result.loss is None
    assert result.discernibility is None
    assert result.loss_metric is None
    assert result.non_uniform_entropy is None
    assert result.class_count is None
    assert result.k_min is None


def test_empty_table_chooses_the_bottom() -> None:
    table = six_rows().iloc[0:0]
    result = search_lattice(table, small_lattice(), (KAnonymity(3),))
    assert result.found
    assert result.node == (0, 0)
    assert result.rows == 0
    assert (result.discernibility, result.loss_metric, result.non_uniform_entropy) == (0, 0.0, 0.0)
    assert result.class_count == 0
    assert result.k_min is None
