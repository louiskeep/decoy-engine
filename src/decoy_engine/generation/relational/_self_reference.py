"""The self-reference draw: a `reference` column whose parent is an earlier row of its own table.

Manager and employee, category and subcategory: the column names a key of the table it sits
in. The rows are drawn as a forest. The first `root_count` rows are roots and hold null; every
later row takes the key of a row above it. A parent always comes earlier, so every chain walks
up to a root and no cycle can form, whatever the seed.

Pattern: the random recursive tree (Smythe and Mahmoud, "A survey of recursive trees", Theory
of Probability and Mathematical Statistics 51, 1995). Node `i` attaches to a node drawn
uniformly from the nodes before it; with more than one root the result is a recursive forest.

With `max_per_parent` the draw is a degree-bounded variant, not the uniform recursive tree the
citation describes: row `i` picks uniformly from the earlier rows that still have room. Those
rows are kept in an array; a row leaves by swap-remove when its last slot is taken, the
constant-time removal of sampling without replacement (Knuth, *TAOCP* vol. 2, sec. 3.4.2), and
each new row joins once its own parent is chosen. The array is never empty: before row `i`
the earlier rows hold `i - root_count` children against `i * max` slots. Each pick is constant
time and nothing is sized by the cap.

`reference_parents` holds the matching rule for the table order: a table waits for the tables
its references name, and never for itself.

The key is read from the table's own columns generated so far, not from a finished parent
table, which is why this is not `_reference`. Config validation requires the key to be an
earlier `sequence` column with no nulls and refuses every option this draw would drop.
"""

from __future__ import annotations

import random
from collections.abc import Container, Iterable
from typing import Any

from decoy_engine.config import is_self_reference
from decoy_engine.generators.derivation import GenDeriveContext

__all__ = ["reference_parents", "self_reference"]


def reference_parents(
    name: str, columns: Iterable[dict[str, Any]], generate_tables: Container[str]
) -> tuple[str, ...]:
    """The generate tables that table `name` must wait for, in first-reference order.

    One entry per parent, at the first `reference` column that names it. A reference to the
    table's own key is a tree inside the table, not a parent, so it adds no entry. A
    `reference_table` that is not a generate table raises a typed `ValueError` here rather
    than a `KeyError` in the draw.
    """
    refs: list[str] = []
    for col in columns:
        if col.get("type") != "reference" or is_self_reference(name, col):
            continue
        ref = col["reference_table"]
        if ref not in generate_tables:
            raise ValueError(
                f"table {name!r} column {col.get('name')!r}: "
                f"reference_table {ref!r} is not a generate table"
            )
        refs.append(ref)
    return tuple(dict.fromkeys(refs))


def _root_rows(col: dict[str, Any], n: int) -> int:
    """How many leading rows are roots: `root_count` (default 1), held inside `1..n`."""
    roots = col.get("root_count")
    return max(1, min(roots if type(roots) is int else 1, n))


def self_reference(
    col: dict[str, Any],
    n: int,
    seed: int,
    derive_key: Any,
    generated: dict[str, list[Any]],
) -> list[Any]:
    """`n` parent keys for the self-reference column `col`: null for a root, else an earlier key.

    `generated` holds the table's columns drawn so far; `col["reference_column"]` names the key
    among them. The per-column seed is the one every generator derives, so the tree is a
    function of the seed and the column config alone.
    """
    keys = generated[col["reference_column"]]
    roots = _root_rows(col, n)
    cap = int(col.get("max_per_parent") or 0)
    rng = random.Random(
        GenDeriveContext.for_column(
            derive_key=derive_key, column_config=col, fallback_seed=seed
        ).base_int("py")
    )
    values: list[Any] = [None] * n
    if cap <= 0:
        for row in range(roots, n):
            values[row] = keys[rng.randrange(row)]
        return values
    open_rows = list(range(roots))
    children = [0] * n
    for row in range(roots, n):
        pick = rng.randrange(len(open_rows))
        parent = open_rows[pick]
        values[row] = keys[parent]
        children[parent] += 1
        if children[parent] >= cap:
            open_rows[pick] = open_rows[-1]
            open_rows.pop()
        open_rows.append(row)
    return values
