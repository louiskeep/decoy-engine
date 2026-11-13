"""Config-time checks for the cross-table options of generate `reference` columns.

Pure functions over plain mappings, returning ``(location, reason code)`` pairs like
`_generate_unique`. The pipeline validator calls them once the reference graph is known to be
valid, so every parent named here exists.

A positive `min_per_parent` or `max_per_parent` asks for an exact shape, and each check refuses
a config that cannot get it rather than letting the run drop the option. One column reports its
first problem only, in this order: `min > max`, a bound beside `distribution: fitted`, a bound
beside `null_probability`, then the row-count ceiling. Zero keeps its meaning of "unset".

Pattern: pigeonhole feasibility. ``P`` parents that take at most ``max`` children each hold
``N`` rows only when ``N <= max * P``. Validate knows the parent table's ``row_count``, an upper
bound on ``P``, so only the maximum can be judged here; the minimum needs the real distinct key
count and is checked by the run (`generation/relational`). The comparison is
``unique_capacity_ok``, the one domain-versus-rows rule (`decoy_engine._capacity`).

A reference to the column's own table is a self-reference: the run draws a tree over the
table's rows (`generation/relational/_self_reference`). `is_self_reference` is the one test for
that shape, and `self_reference_problems` holds its rules, so `per_parent_problems` leaves such
a column alone. A tree needs a key that is distinct and never null before the column is drawn,
so the key must be an earlier `sequence` column of the same table with no `null_probability`.
The tree copies the key's drawn values and a reference cannot declare `output_type`, so a key
that declares one is refused: its cast values would match no copied foreign key.
The draw is uniform over earlier rows with an optional `max_per_parent`; any other distribution,
a minimum, nulls or uniqueness would be dropped without a word, so each is refused. `root_count`
must lie between 1 and the table's `row_count`, and is ignored for a table with no rows.

A problem carries its reason code only: a bound, weight or key never reaches a message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from decoy_engine import _capacity, reasons
from decoy_engine.config._tables import generate_columns_of

__all__ = ["is_self_reference", "per_parent_problems", "self_reference_problems"]

Problem = tuple[tuple[str | int, ...], str]

_BOUNDS_CONFLICT = str(reasons.config.GENERATE_PER_PARENT_BOUNDS_CONFLICT)
_FITTED_CONFLICT = str(reasons.config.GENERATE_FITTED_REFERENCE_BOUNDS_CONFLICT)
_NULL_CONFLICT = str(reasons.config.GENERATE_PER_PARENT_NULL_CONFLICT)
_INFEASIBLE = str(reasons.config.GENERATE_PER_PARENT_INFEASIBLE)
_KEY_INVALID = str(reasons.config.GENERATE_SELF_REFERENCE_KEY_INVALID)
_OPTION_UNSUPPORTED = str(reasons.config.GENERATE_SELF_REFERENCE_OPTION_UNSUPPORTED)
_RANGE = str(reasons.config.GENERATE_OPTION_RANGE)
_UNKNOWN = str(reasons.config.GENERATE_OPTION_UNKNOWN)


def _positive(value: Any) -> int:
    """A bound as a positive int, or 0 for absent, zero, or anything the registry refuses."""
    return value if type(value) is int and value > 0 else 0


def _row_count(table: Mapping[str, Any] | None) -> int | None:
    rows = table.get("row_count") if table is not None else None
    return rows if type(rows) is int else None


def _nulls_on(value: Any) -> bool:
    return type(value) in (int, float) and value > 0


def _column_problem(
    col: Mapping[str, Any], rows: int | None, parent_rows: int | None
) -> tuple[str, str] | None:
    """The first `(option, code)` problem of one reference column, or None."""
    lo, hi = _positive(col.get("min_per_parent")), _positive(col.get("max_per_parent"))
    if not lo and not hi:
        return None
    if lo > hi > 0:
        return ("min_per_parent", _BOUNDS_CONFLICT)
    if col.get("distribution") == "fitted":
        return ("distribution", _FITTED_CONFLICT)
    if _nulls_on(col.get("null_probability")):
        return ("null_probability", _NULL_CONFLICT)
    # An empty parent or child is not a capacity question: the run's empty-parent rule decides.
    if hi and rows and parent_rows and not _capacity.unique_capacity_ok(hi * parent_rows, rows):
        return ("max_per_parent", _INFEASIBLE)
    return None


def per_parent_problems(tables: Sequence[Mapping[str, Any]]) -> list[Problem]:
    """Per-parent bound problems of every generate `reference` column in `tables`.

    `tables` are the config's tables as mappings with `name`, `row_count` and
    `generate_columns`. A parent with no integer `row_count` is skipped for the ceiling; the
    run checks the real key count either way.
    """
    by_name = {t.get("name"): t for t in tables}
    problems: list[Problem] = []
    for t_idx, table in enumerate(tables):
        rows = _row_count(table)
        for c_idx, col in enumerate(generate_columns_of(table)):
            if not isinstance(col, Mapping) or col.get("type") != "reference":
                continue
            if is_self_reference(table.get("name"), col):
                continue
            parent = col.get("reference_table")
            parent_rows = _row_count(by_name.get(parent)) if isinstance(parent, str) else None
            found = _column_problem(col, rows, parent_rows)
            if found is not None:
                problems.append((("tables", t_idx, "generate_columns", c_idx, found[0]), found[1]))
    return problems


def is_self_reference(table_name: Any, col: Mapping[str, Any]) -> bool:
    """Whether `col`, a column of the table named `table_name`, references its own table."""
    return (
        col.get("type") == "reference"
        and isinstance(table_name, str)
        and bool(table_name)
        and col.get("reference_table") == table_name
    )


def _key_is_valid(columns: Sequence[Any], c_idx: int, key_name: Any) -> bool:
    """Whether `key_name` is an uncast `sequence` column declared before `c_idx` with no nulls."""
    for earlier in columns[:c_idx]:
        if isinstance(earlier, Mapping) and earlier.get("name") == key_name:
            return (
                earlier.get("type") == "sequence"
                and not _nulls_on(earlier.get("null_probability"))
                and "output_type" not in earlier
            )
    return False


def _tree_problem(
    col: Mapping[str, Any], columns: Sequence[Any], c_idx: int, rows: int | None
) -> tuple[str, str] | None:
    """The first `(option, code)` problem of one self-reference column, or None."""
    if not _key_is_valid(columns, c_idx, col.get("reference_column")):
        return ("reference_column", _KEY_INVALID)
    if col.get("distribution") not in (None, "random"):
        return ("distribution", _OPTION_UNSUPPORTED)
    if _positive(col.get("min_per_parent")):
        return ("min_per_parent", _OPTION_UNSUPPORTED)
    if _nulls_on(col.get("null_probability")):
        return ("null_probability", _OPTION_UNSUPPORTED)
    if col.get("unique") is True:
        return ("unique", _OPTION_UNSUPPORTED)
    roots = col.get("root_count")
    # A table with no rows has no roots to count, so the option is not judged there.
    if type(roots) is int and rows and not 1 <= roots <= rows:
        return ("root_count", _RANGE)
    return None


def self_reference_problems(tables: Sequence[Mapping[str, Any]]) -> list[Problem]:
    """Self-reference problems of every generate `reference` column in `tables`.

    `tables` are the config's tables as mappings with `name`, `row_count`, `generate_columns`
    and `unique_together`. `root_count` belongs to a self-reference only, so on a reference to
    another table it is an unknown option. A self-reference cannot be a `unique_together`
    member: the pair draw reads a finished parent table, and this one is still being drawn.
    """
    problems: list[Problem] = []
    for t_idx, table in enumerate(tables):
        name, rows = table.get("name"), _row_count(table)
        columns = generate_columns_of(table)
        pairs = table.get("unique_together") or []
        paired = {m for pair in pairs if isinstance(pair, (list, tuple)) for m in pair}
        for c_idx, col in enumerate(columns):
            if not isinstance(col, Mapping) or col.get("type") != "reference":
                continue
            where: tuple[str | int, ...] = ("tables", t_idx, "generate_columns", c_idx)
            if not is_self_reference(name, col):
                if "root_count" in col:
                    problems.append(((*where, "root_count"), _UNKNOWN))
                continue
            if col.get("name") in paired:
                problems.append((("tables", t_idx, "unique_together"), _OPTION_UNSUPPORTED))
                continue
            found = _tree_problem(col, columns, c_idx, rows)
            if found is not None:
                problems.append(((*where, found[0]), found[1]))
    return problems
