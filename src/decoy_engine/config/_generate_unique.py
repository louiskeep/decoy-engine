"""Config-time checks for `unique` generate columns and `unique_together` junction pairs.

Pure functions over plain mappings, returning ``(location, reason code)`` pairs like the option
registry (`_generate_options`). Nothing here is wired into a validator yet: `unique` is not a
registered option and `TableConfig` has no `unique_together`, so no accepted config reaches these
checks. The wiring slice registers the option and calls them from one table validator
(`table_unique_problems`) and one pipeline validator (`cross_table_unique_problems`).

Validate-time size checks use provable upper bounds only (a category count, a parent table's
``row_count``); the real distinct count of a parent key column is known only after the parent is
generated, and the run re-checks it. Every size comparison goes through
``unique_capacity_ok(domain, rows, null_reusable=...)``, the one rule the masking pool also uses,
so there is no second domain-versus-rows comparison.

A problem carries its reason code only. The caller puts the table, column and type in the
message; a category, key or any submitted value never reaches one.

Pair semantics follow SQL ``UNIQUE (a, b)`` (Django's ``unique_together``). SDV expresses the
same intent as a composite key on the bridge table.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from decoy_engine import reasons

__all__ = [
    "UNIQUE_FAKER_ATTEMPTS",
    "UNIQUE_TYPES",
    "Problem",
    "column_conflict_problems",
    "cross_table_unique_problems",
    "distinct_categories",
    "table_unique_problems",
    "unique_flag",
    "unique_type_problems",
    "value_key",
]

Problem = tuple[tuple[str | int, ...], str]

_CONFLICT = str(reasons.config.GENERATE_UNIQUE_CONFLICT)
_TOO_SMALL = str(reasons.config.GENERATE_UNIQUE_DOMAIN_TOO_SMALL)
_UNSUPPORTED = str(reasons.config.GENERATE_UNIQUE_UNSUPPORTED)
_TYPE = str(reasons.config.GENERATE_OPTION_TYPE)

# Faker's own `_UNIQUE_ATTEMPTS`: draws allowed for one row of a unique faker column before the
# domain counts as spent. The one owner; the draw, its native provider and the draw-site
# catalogue all read it from here.
UNIQUE_FAKER_ATTEMPTS = 1000

# The four types whose domain is enumerable or that are distinct by construction.
UNIQUE_TYPES = frozenset({"sequence", "categorical", "faker", "reference"})

# Unique-capable types whose values an `output_type` cast can merge.
_LOSSY_OUTPUT_TYPES = frozenset({"categorical", "faker"})

# A reference distribution that picks parents with replacement or by weight.
_REFERENCE_CONFLICT_DISTRIBUTIONS = frozenset({"weighted", "fitted"})
# Options a pair member may not set: the pair draw replaces the member's own sampling and skips
# the per-column null post-pass.
_PAIR_MEMBER_BLOCKED = ("unique", "null_probability", "weights", "min_per_parent", "max_per_parent")


def _capacity_ok(domain: int, rows: int, *, null_reusable: bool = False) -> bool:
    # Imported here: `generation.pool` pulls pandas and the provider registry, and the config
    # layer stays light until a unique column actually needs the shared rule.
    from decoy_engine.generation.pool._capacity import unique_capacity_ok

    return unique_capacity_ok(domain, rows, null_reusable=null_reusable)


def unique_flag(value: Any, _: Mapping[str, Any]) -> list[Problem]:
    """`unique` is a real bool. `"true"`, `1` and `0` are refused rather than coerced."""
    return [] if type(value) is bool else [((), _TYPE)]


def _is_on(extras: Mapping[str, Any]) -> bool:
    return extras.get("unique") is True


def unique_type_problems(type_: str, extras: Mapping[str, Any]) -> list[Problem]:
    """`unique: true` on a type outside `UNIQUE_TYPES`. `unique: false` and absent are fine."""
    if _is_on(extras) and type_ not in UNIQUE_TYPES:
        return [(("unique",), _UNSUPPORTED)]
    return []


def column_conflict_problems(
    type_: str, extras: Mapping[str, Any], *, pooled: bool | None = None
) -> list[Problem]:
    """Options that cannot combine with `unique: true` on one column.

    `pooled` is a declared field, not an extra, so the caller passes it. A column that does not
    set `unique: true` has no conflict.
    """
    if not _is_on(extras) or type_ not in UNIQUE_TYPES:
        return []
    problems: list[Problem] = []
    if pooled is True:
        problems.append((("pooled",), _CONFLICT))
    if "output_type" in extras and type_ in _LOSSY_OUTPUT_TYPES:
        # The cast runs after the distinctness check, so "1" and "01" (int), "true" and "TRUE"
        # (bool) or 1.2 and 1.4 (decimal(0)) can land on one value. A sequence never collides:
        # its step is a nonzero integer, so int and decimal(n) keep its values apart.
        problems.append((("output_type",), _CONFLICT))
    if type_ == "categorical" and extras.get("weights") is not None:
        problems.append((("weights",), _CONFLICT))
    if type_ == "reference":
        if extras.get("distribution") in _REFERENCE_CONFLICT_DISTRIBUTIONS:
            problems.append((("distribution",), _CONFLICT))
        for key in ("min_per_parent", "max_per_parent"):
            value = extras.get(key)
            if type(value) is int and value > 0:
                problems.append(((key,), _CONFLICT))
    return problems


def value_key(value: Any) -> Any:
    """A hashable key that is equal for exactly the values that compare equal.

    The one rule every unique draw uses to tell values apart, so a list or dict (a schema-valid
    category, parent key or provider output) is tracked by equality instead of raising
    `TypeError` from a `set`. Lists and tuples become tagged tuples of their keys, a dict a
    frozenset of key pairs (order-free, like dict equality); any other value is its own key.
    A value with an unhashable leaf raises `TypeError`; callers turn that into a typed refusal.
    """
    if isinstance(value, list):
        return ("list", tuple(value_key(v) for v in value))
    if isinstance(value, tuple):
        return ("tuple", tuple(value_key(v) for v in value))
    if isinstance(value, dict):
        return ("dict", frozenset((value_key(k), value_key(v)) for k, v in value.items()))
    hash(value)
    return value


def distinct_categories(categories: Sequence[Any]) -> tuple[list[Any], bool]:
    """Distinct non-null categories in first-seen order, and whether `None` is among them.

    Keyed by `value_key`, never a bare `set()`, so a category that is not hashable (a list or
    dict the config allows) does not raise `TypeError`. `None` is a reusable SQL null, not a
    colliding value, so it is reported apart.
    """
    distinct: list[Any] = []
    seen: set[Any] = set()
    has_null = False
    for category in categories:
        if category is None:
            has_null = True
            continue
        key = value_key(category)
        if key not in seen:
            seen.add(key)
            distinct.append(category)
    return distinct, has_null


def _row_count(table: Mapping[str, Any]) -> int | None:
    rows = table.get("row_count")
    return rows if type(rows) is int else None


def _columns(table: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [c for c in table.get("generate_columns") or [] if isinstance(c, Mapping)]


def _is_set(value: Any) -> bool:
    """An option counts as set unless it is absent or its explicit off value (0, False, None)."""
    return (
        value is not None
        and value is not False
        and not (type(value) in (int, float) and value == 0)
    )


def _pair_member_blocked(member: Mapping[str, Any]) -> bool:
    if any(_is_set(member.get(key)) for key in _PAIR_MEMBER_BLOCKED):
        return True
    if member.get("determinism") == "fresh":
        return True
    return member.get("distribution", "random") != "random"


def _pair_problems(columns: Sequence[Mapping[str, Any]], pairs: Sequence[Any]) -> list[Problem]:
    """Shape rules for `unique_together`: two distinct reference columns of this table."""
    by_name = {c.get("name"): c for c in columns}
    problems: list[Problem] = []
    claimed: set[str] = set()
    for i, pair in enumerate(pairs):
        names = pair if isinstance(pair, (list, tuple)) else None
        if names is None or len(names) != 2 or not all(isinstance(n, str) for n in names):
            problems.append((("unique_together", i), _CONFLICT))
            continue
        if names[0] == names[1] or any(n in claimed for n in names):
            problems.append((("unique_together", i), _CONFLICT))
            continue
        claimed |= {*names}
        for j, name in enumerate(names):
            member = by_name.get(name)
            if member is None or member.get("type") != "reference" or _pair_member_blocked(member):
                problems.append((("unique_together", i, j), _CONFLICT))
    return problems


def table_unique_problems(table: Mapping[str, Any]) -> list[Problem]:
    """Per-table checks: `unique_together` shape and categorical domain sizes.

    `table` is a generate table as a mapping with `row_count`, `generate_columns` and optionally
    `unique_together`. A reference column's domain needs the parent table, so that check is in
    `cross_table_unique_problems`.
    """
    columns = _columns(table)
    problems = _pair_problems(columns, table.get("unique_together") or [])
    rows = _row_count(table)
    if rows is None:
        return problems
    for idx, col in enumerate(columns):
        if col.get("type") != "categorical" or col.get("unique") is not True:
            continue
        categories = col.get("categories")
        if not isinstance(categories, list):
            continue
        distinct, has_null = distinct_categories(categories)
        if not _capacity_ok(len(distinct), rows, null_reusable=has_null):
            problems.append((("generate_columns", idx), _TOO_SMALL))
    return problems


def _pair_domain_bound(
    pair: Any, by_name: Mapping[Any, Mapping[str, Any]], rows_by_name: Mapping[Any, int | None]
) -> int | None:
    """Product of the two parents' row counts; None when either is unknown or the pair is malformed."""
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return None
    if not all(isinstance(name, str) for name in pair):
        return None
    bound = 1
    for name in pair:
        member = by_name.get(name)
        if member is None or member.get("type") != "reference":
            return None
        parent = member.get("reference_table")
        parent_rows = rows_by_name.get(parent) if isinstance(parent, str) else None
        if parent_rows is None:
            return None
        bound *= parent_rows
    return bound


def cross_table_unique_problems(tables: Sequence[Mapping[str, Any]]) -> list[Problem]:
    """Cross-table checks against each parent's `row_count`, an upper bound on its key count.

    A child with more rows than its parent has keys cannot hold distinct references, and a pair
    cannot exceed the product of its two parents. A parent that is not a generate table here, or
    has no integer `row_count`, is skipped: the run re-checks the real distinct key count.
    """
    rows_by_name = {t.get("name"): _row_count(t) for t in tables}
    problems: list[Problem] = []
    for t_idx, table in enumerate(tables):
        rows = _row_count(table)
        if rows is None:
            continue
        columns = _columns(table)
        by_name = {c.get("name"): c for c in columns}
        for c_idx, col in enumerate(columns):
            if col.get("type") != "reference" or col.get("unique") is not True:
                continue
            parent = col.get("reference_table")
            parent_rows = rows_by_name.get(parent) if isinstance(parent, str) else None
            if parent_rows is not None and not _capacity_ok(parent_rows, rows):
                problems.append((("tables", t_idx, "generate_columns", c_idx), _TOO_SMALL))
        for p_idx, pair in enumerate(table.get("unique_together") or []):
            bound = _pair_domain_bound(pair, by_name, rows_by_name)
            if bound is not None and not _capacity_ok(bound, rows):
                problems.append((("tables", t_idx, "unique_together", p_idx), _TOO_SMALL))
    return problems
