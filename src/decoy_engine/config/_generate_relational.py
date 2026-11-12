"""Config-time checks for the cross-table options of generate `reference` columns.

Pure functions over plain mappings, returning ``(location, reason code)`` pairs like
`_generate_unique`. The pipeline validator calls `per_parent_problems` once the reference graph
is known to be valid, so every parent named here exists.

A positive `min_per_parent` or `max_per_parent` asks for an exact shape, and each check refuses
a config that cannot get it rather than letting the run drop the option. One column reports its
first problem only, in this order: `min > max`, a bound beside `distribution: fitted`, a bound
beside `null_probability`, then the row-count ceiling. Zero keeps its meaning of "unset".

Pattern: pigeonhole feasibility. ``P`` parents that take at most ``max`` children each hold
``N`` rows only when ``N <= max * P``. Validate knows the parent table's ``row_count``, an upper
bound on ``P``, so only the maximum can be judged here; the minimum needs the real distinct key
count and is checked by the run (`generation/relational`). The comparison is
``unique_capacity_ok``, the one domain-versus-rows rule (`decoy_engine._capacity`).

A problem carries its reason code only: a bound, weight or key never reaches a message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from decoy_engine import _capacity, reasons

__all__ = ["per_parent_problems"]

Problem = tuple[tuple[str | int, ...], str]

_BOUNDS_CONFLICT = str(reasons.config.GENERATE_PER_PARENT_BOUNDS_CONFLICT)
_FITTED_CONFLICT = str(reasons.config.GENERATE_FITTED_REFERENCE_BOUNDS_CONFLICT)
_NULL_CONFLICT = str(reasons.config.GENERATE_PER_PARENT_NULL_CONFLICT)
_INFEASIBLE = str(reasons.config.GENERATE_PER_PARENT_INFEASIBLE)


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
        for c_idx, col in enumerate(table.get("generate_columns") or []):
            if not isinstance(col, Mapping) or col.get("type") != "reference":
                continue
            parent = col.get("reference_table")
            parent_rows = _row_count(by_name.get(parent)) if isinstance(parent, str) else None
            found = _column_problem(col, rows, parent_rows)
            if found is not None:
                problems.append((("tables", t_idx, "generate_columns", c_idx, found[0]), found[1]))
    return problems
