"""The one lookup from a child's foreign key values to their parent rows.

A generated child column may read a column of the row its foreign key points at: a date
anchored to the parent's date, a value copied from the parent. Every such read goes through
`lookup_parent_values`, so the rules cannot differ between features:

- a null foreign key reads nothing and gives a null;
- a non-null foreign key that matches a parent row gives that row's value, null included: the
  result records that the row was found, so a caller can tell a parent's null value from a
  null foreign key;
- a non-null foreign key with no parent row is refused, never read as null;
- a parent key that occurs twice is refused: the row a child means would be ambiguous.

Pattern: a hash join with the parent as the build side and the child's keys as the probe side
(the classic hash join; Garcia-Molina, Ullman and Widom, *Database Systems: The Complete Book*,
sec. 15.5), with the SQL rule that a null key matches nothing. The build key is
`config.value_key`, the engine's one equality-normalizing key, so a list or dict key is matched
by value. The selected parent column's Arrow type is returned with the values, so a copied
column keeps its type even when every value is null.

Refusals are the registered summary plus the child table and column names. No key or value
reaches a message, and each is raised outside any `except` block.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.config import value_key
from decoy_engine.generation.pool import GenerationError

__all__ = ["ParentLookupResult", "lookup_parent_values"]


@dataclass(frozen=True)
class ParentLookupResult:
    """One parent value per foreign key, whether its parent row was found, and the column type.

    `found[i]` is False only for a null foreign key. A found row whose value is null keeps
    `found[i]` True and `values[i]` None.
    """

    values: list[Any]
    found: list[bool]
    dtype: pa.DataType


def _key(value: Any) -> tuple[bool, Any]:
    """`(True, key)` for a value that has an equality key, else `(False, None)`."""
    try:
        return True, value_key(value)
    except TypeError:
        return False, None


def lookup_parent_values(
    pools: dict[str, pa.Table],
    *,
    parent_table: str,
    key_column: str,
    value_column: str,
    foreign_keys: list[Any],
    child_table: str,
    child_column: str,
) -> ParentLookupResult:
    """The `value_column` cell of the `parent_table` row each foreign key points at.

    `pools` holds the finished parent tables. `child_table` and `child_column` name the foreign
    key column, for a refusal's text only. Raises `GenerationError` with
    `runtime.parent_lookup_key_not_unique` when a parent key repeats and with
    `runtime.parent_lookup_key_missing` when a non-null foreign key matches no parent row.
    """
    where = f" Table {child_table!r}, column {child_column!r}."
    parent = pools[parent_table]
    cells = parent.column(value_column).to_pylist()
    row_of: dict[Any, int] = {}
    repeated = False
    for row, parent_key in enumerate(parent.column(key_column).to_pylist()):
        if parent_key is None:
            continue
        # Arrow values come back as plain builtins, so every parent key is keyable.
        key = value_key(parent_key)
        repeated = repeated or key in row_of
        row_of[key] = row
    if repeated:
        raise GenerationError(
            code=reasons.runtime.PARENT_LOOKUP_KEY_NOT_UNIQUE,
            message=f"{reasons.runtime.PARENT_LOOKUP_KEY_NOT_UNIQUE.spec.summary}{where}",
        )
    values: list[Any] = []
    found: list[bool] = []
    missing = False
    for foreign_key in foreign_keys:
        if foreign_key is None:
            values.append(None)
            found.append(False)
            continue
        keyable, key = _key(foreign_key)
        hit = row_of.get(key) if keyable else None
        if hit is None:
            missing = True
            break
        values.append(cells[hit])
        found.append(True)
    if missing:
        raise GenerationError(
            code=reasons.runtime.PARENT_LOOKUP_KEY_MISSING,
            message=f"{reasons.runtime.PARENT_LOOKUP_KEY_MISSING.spec.summary}{where}",
        )
    return ParentLookupResult(values, found, parent.schema.field(value_column).type)
