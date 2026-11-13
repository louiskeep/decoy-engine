"""The conditional `categorical` column: a category mix per value of an earlier column (`by:`).

Pattern: a conditional probability table (CPT) with one parent, sampled ancestrally in declared
column order (Koller and Friedman, *Probabilistic Graphical Models*, 2009, ch. 3 and sec. 12.1).
The parent column is generated first; each row then draws from the table its parent value
selects. A parent value with no `by` entry draws from the column's top-level `categories` and
`weights`, as a fitted `condition_on` falls back to its marginal.

This module builds the tables and fetches the parent values; it draws nothing. The draw stays at
the one categorical draw site (`synthesize._categorical`): the column's one `random.Random` and
one `choices` call per row, so the column consumes as many draws as a plain column of the same
length. Each table's cumulative weights are built once per column with `itertools.accumulate`,
which is what `choices` computes from `weights` on every call, so passing them gives the same
bytes. Rows are matched to a table only by `statistical.select_table` (`str(parent)`), the
fitted case's rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import accumulate
from typing import Any

from decoy_engine import reasons
from decoy_engine.generation.pool import GenerationError
from decoy_engine.generators.derivation import TABLE_KEY

__all__ = ["CategoricalTable", "condition_values", "conditional_tables"]

# The categories and their cumulative weights; None draws uniformly, as a plain column without
# `weights` does.
CategoricalTable = tuple[list[Any], list[float] | None]


def _table(categories: Sequence[Any], weights: Sequence[float] | None) -> CategoricalTable:
    return list(categories), None if weights is None else list(accumulate(weights))


def conditional_tables(
    col: Mapping[str, Any],
) -> tuple[CategoricalTable, dict[str, CategoricalTable]]:
    """The top-level table and one table per `by` key of a validated conditional column.

    An entry with only `weights` reweights the top-level categories; an entry with `categories`
    draws from its own list, weighted only by its own `weights`.
    """
    cats = col.get("categories", ["Category A", "Category B"])
    marginal = _table(cats, col.get("weights"))
    tables: dict[str, CategoricalTable] = {}
    for key, entry in (col.get("by") or {}).items():
        tables[key] = _table(entry.get("categories", cats), entry.get("weights"))
    return marginal, tables


def condition_values(
    col: Mapping[str, Any], generated: Mapping[str, list[Any]] | None
) -> list[Any]:
    """The generated values of the column `condition_on` names.

    A missing column is a compile bypass, not an unseen parent: it is refused before any row is
    selected, so the rows never all fall back to the top-level mix in silence. The message names
    the table, the column and the option only.
    """
    name = col.get("condition_on")
    if generated is None or not isinstance(name, str) or name not in generated:
        summary = reasons.runtime.CATEGORICAL_CONDITION_COLUMN_UNAVAILABLE.spec.summary
        where = f" Table {col.get(TABLE_KEY)!r}, column {col.get('name')!r}, option 'condition_on'."
        raise GenerationError(
            code=reasons.runtime.CATEGORICAL_CONDITION_COLUMN_UNAVAILABLE,
            message=f"{summary}{where}",
        )
    return generated[name]
