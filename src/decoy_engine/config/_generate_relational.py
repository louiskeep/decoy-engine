"""Cross-table generate options: their config-time checks and the resolver of column inputs.

The checks are pure functions over plain mappings, returning ``(location, reason code)`` pairs
like `_generate_unique`. The pipeline validator calls them once the reference graph is known to
be valid, so every parent named here exists.

`iter_generate_input_refs` resolves which column each option of a generate column reads. It
holds no option name and no column type list: it walks the ``ref_kind`` descriptors of the one
registry (`_generate_options.GENERATE_OPTIONS`) and follows a `reference` column to its table.
The plan check, numeric typing and the reproducibility report consume its edges.

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
The draw is uniform over earlier rows with an optional `max_per_parent`; any other distribution,
a minimum, nulls or uniqueness would be dropped without a word, so each is refused. `root_count`
must lie between 1 and the table's `row_count`, and is ignored for a table with no rows.

A column that reads its parent's row (a `windowed_date` with `anchor_via`) needs a foreign key
that reaches exactly one parent row. `parent_key_problem` is the one rule for such a key, a
`sequence` column with no `null_probability`; the tree's key goes through it too. The lookup
column must be a `reference` to another table: the rows of the table being drawn are not
finished, so a lookup through a self-reference is refused.

A `derived_aggregate` with `from_table` is a per-parent aggregate: `via` names the child's
`reference` back to this table, and each parent row gets `op` over its own children. The child
key must reach exactly one parent row, so the referenced key goes through `parent_key_problem`
as an earlier column. The run fills the column only after every table exists, so any input
that resolves to one (a same-table reader, a child's anchor, another aggregate, a reference) is
refused with `config.generate_aggregate_read_before_fill`: it would read the placeholder, not
the value. `null_probability` on it is refused because the fill would overwrite the nulls.
The child column must hold numbers as numeric typing types it for an aggregate, a reader that
keeps it text: its declared `output_type`, else the type its generator makes as text
(`_generate_type_checks.numeric_as_text_read`, after `reference_root`). Any other column is
refused with `config.generate_aggregate_input_not_numeric` before any data is read, so a
validated config never sums text.

A problem carries its reason code only: a bound, weight or key never reaches a message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from decoy_engine import _capacity, reasons
from decoy_engine.config._generate_options import GENERATE_OPTIONS, OptionSpec
from decoy_engine.config._generate_type_checks import numeric_as_text_read
from decoy_engine.config._tables import generate_columns_of
from decoy_engine.expressions import compile_expr

__all__ = [
    "GenerateInputRef",
    "aggregate_problems",
    "is_per_parent_aggregate",
    "is_self_reference",
    "iter_generate_input_refs",
    "parent_key_problem",
    "parent_lookup_problems",
    "per_parent_problems",
    "reference_root",
    "self_reference_problems",
]

Problem = tuple[tuple[str | int, ...], str]
# A problem whose message adds value-free names to the code's summary.
NamedProblem = tuple[tuple[str | int, ...], str, str]

_BOUNDS_CONFLICT = str(reasons.config.GENERATE_PER_PARENT_BOUNDS_CONFLICT)
_FITTED_CONFLICT = str(reasons.config.GENERATE_FITTED_REFERENCE_BOUNDS_CONFLICT)
_NULL_CONFLICT = str(reasons.config.GENERATE_PER_PARENT_NULL_CONFLICT)
_INFEASIBLE = str(reasons.config.GENERATE_PER_PARENT_INFEASIBLE)
_KEY_INVALID = str(reasons.config.GENERATE_SELF_REFERENCE_KEY_INVALID)
_OPTION_UNSUPPORTED = str(reasons.config.GENERATE_SELF_REFERENCE_OPTION_UNSUPPORTED)
_LOOKUP_KEY = str(reasons.config.GENERATE_PARENT_LOOKUP_KEY_NOT_UNIQUE)
_LOOKUP_SELF = str(reasons.config.GENERATE_PARENT_LOOKUP_SELF_REFERENCE)
_RANGE = str(reasons.config.GENERATE_OPTION_RANGE)
_UNKNOWN = str(reasons.config.GENERATE_OPTION_UNKNOWN)
_READ_BEFORE_FILL = str(reasons.config.GENERATE_AGGREGATE_READ_BEFORE_FILL)
_NOT_NUMERIC = str(reasons.config.GENERATE_AGGREGATE_INPUT_NOT_NUMERIC)


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


def parent_key_problem(columns: Sequence[Any], key_name: Any, code: str) -> str | None:
    """`code` unless `key_name` is a `sequence` column of `columns` that injects no nulls.

    The one rule for a key that reaches exactly one row: distinct by construction and never
    null. Each caller passes the columns the key may come from and its own reason code.
    """
    for candidate in columns:
        if isinstance(candidate, Mapping) and candidate.get("name") == key_name:
            unique = candidate.get("type") == "sequence" and not _nulls_on(
                candidate.get("null_probability")
            )
            return None if unique else code
    return code


def _tree_problem(
    col: Mapping[str, Any], columns: Sequence[Any], c_idx: int, rows: int | None
) -> tuple[str, str] | None:
    """The first `(option, code)` problem of one self-reference column, or None."""
    # A tree's key is read while the table is drawn, so only an earlier column can be one.
    if parent_key_problem(columns[:c_idx], col.get("reference_column"), _KEY_INVALID):
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


def _lookup_problem(
    via: Mapping[str, Any], table_name: Any, by_name: Mapping[Any, Mapping[str, Any]]
) -> str | None:
    """The reason code a lookup through the column `via` of table `table_name` is refused with.

    A column that is not a `reference` names no parent table, so it has no parent key either.
    """
    if is_self_reference(table_name, via):
        return _LOOKUP_SELF
    parent = by_name.get(via.get("reference_table")) if via.get("type") == "reference" else None
    columns = generate_columns_of(parent) if parent is not None else []
    return parent_key_problem(columns, via.get("reference_column"), _LOOKUP_KEY)


def parent_lookup_problems(tables: Sequence[Mapping[str, Any]]) -> list[Problem]:
    """Parent-lookup problems of every generate column of `tables` that has `anchor_via`.

    `tables` are the config's tables as mappings with `name` and `generate_columns`. A lookup
    column that is missing is left alone: which column exists, and in what order, is the plan
    check's rule over the resolved inputs.
    """
    by_name = {t.get("name"): t for t in tables}
    problems: list[Problem] = []
    for t_idx, table in enumerate(tables):
        columns = [c for c in generate_columns_of(table) if isinstance(c, Mapping)]
        named = {c.get("name"): c for c in columns}
        for c_idx, col in enumerate(columns):
            via_name = col.get("anchor_via")
            if not isinstance(via_name, str) or via_name not in named:
                continue
            code = _lookup_problem(named[via_name], table.get("name"), by_name)
            if code is not None:
                problems.append((("tables", t_idx, "generate_columns", c_idx, "anchor_via"), code))
    return problems


def is_per_parent_aggregate(col: Mapping[str, Any]) -> bool:
    """Whether `col` is a `derived_aggregate` filled per parent row from another table."""
    return col.get("type") == "derived_aggregate" and col.get("from_table") is not None


def _aggregate_problem(
    col: Mapping[str, Any],
    table_name: Any,
    earlier: Sequence[Any],
    by_name: Mapping[Any, Mapping[str, Any]],
) -> tuple[str, str] | None:
    """The first `(option, code)` problem of one `derived_aggregate` column, or None.

    A child table or `via` column that is missing is left to the plan check, which owns where
    an input must exist.
    """
    if not is_per_parent_aggregate(col):
        return ("via", _UNKNOWN) if col.get("via") is not None else None
    if col.get("via") is None:
        return ("via", "missing")
    if _nulls_on(col.get("null_probability")):
        return ("null_probability", _NULL_CONFLICT)
    child = by_name.get(col.get("from_table"))
    columns = generate_columns_of(child) if child is not None else []
    via = next((c for c in columns if isinstance(c, Mapping) and c.get("name") == col["via"]), None)
    if via is not None:
        if is_self_reference(child.get("name") if child else None, via):
            return ("via", _LOOKUP_SELF)
        if via.get("type") != "reference" or via.get("reference_table") != table_name:
            return ("via", _LOOKUP_KEY)
        # The key is the parent table's own column, finished before the post-pass, but the rule
        # is the one every parent lookup follows: an earlier non-null `sequence`.
        if parent_key_problem(earlier, via.get("reference_column"), _LOOKUP_KEY):
            return ("via", _LOOKUP_KEY)
    if not _input_numeric(by_name, col.get("from_table"), col.get("column")):
        return ("column", _NOT_NUMERIC)
    return None


def _column_of(table: Mapping[str, Any] | None, name: Any) -> Mapping[str, Any] | None:
    columns = generate_columns_of(table) if table is not None else []
    return next((c for c in columns if isinstance(c, Mapping) and c.get("name") == name), None)


def reference_root(
    tables: Mapping[Any, Mapping[str, Any]], key: tuple[str, str]
) -> tuple[str, str]:
    """Follow `reference` columns from `key` to the column that decides the values.

    `tables` maps a table name to its config; a cycle stops where it loops.
    """
    seen: set[tuple[str, str]] = set()
    while key not in seen:
        seen.add(key)
        col = _column_of(tables.get(key[0]), key[1])
        if col is None or col.get("type") != "reference":
            return key
        key = (str(col.get("reference_table")), str(col.get("reference_column")))
    return key


def _input_numeric(by_name: Mapping[Any, Mapping[str, Any]], child: Any, column: Any) -> bool:
    """Whether the child column a per-parent aggregate reads holds numbers when it is read.

    A missing child column is the plan check's refusal, and another per-parent aggregate is
    `generate_aggregate_read_before_fill`'s, not this one.
    """
    if not isinstance(child, str) or not isinstance(column, str):
        return True
    if _column_of(by_name.get(child), column) is None:
        return True
    root = reference_root(by_name, (child, column))
    source = _column_of(by_name.get(root[0]), root[1])
    if source is not None and is_per_parent_aggregate(source):
        return True
    return source is not None and numeric_as_text_read(source)


def _named(
    where: tuple[str | int, ...], col: Mapping[str, Any], found: tuple[str, str]
) -> Problem | NamedProblem:
    """The problem for `found`, with the child table, column and aggregate named for the input."""
    if found[1] != _NOT_NUMERIC:
        return ((*where, found[0]), found[1])
    names = (str(col.get("from_table")), str(col.get("column")), str(col.get("name")))
    detail = "Table {!r}, column {!r}, aggregate {!r}.".format(*names)
    return ((*where, found[0]), found[1], detail)


def _reads_aggregate(
    table: Mapping[str, Any], col: Mapping[str, Any], filled_late: set[tuple[Any, Any]]
) -> str | None:
    """The option of `col` that reads a per-parent aggregate, or None."""
    for edge in iter_generate_input_refs(table, col):
        if (edge.source_table, edge.source_column) in filled_late:
            return edge.option
    # A reference copies its parent column while drawing, before any post-pass.
    if col.get("type") == "reference":
        pair = (col.get("reference_table"), col.get("reference_column"))
        return "reference_column" if pair in filled_late else None
    return None


def aggregate_problems(tables: Sequence[Mapping[str, Any]]) -> list[Problem | NamedProblem]:
    """Per-parent aggregate problems of every generate column of `tables`.

    `tables` are the config's tables as mappings with `name` and `generate_columns`. Each
    `derived_aggregate` is checked for its companion keys, nulls, lookup key and numeric input;
    every other column, in any table, is checked for an input that resolves to a per-parent
    aggregate. A non-numeric input's problem also names the child table, column and aggregate.
    """
    by_name = {t.get("name"): t for t in tables}
    filled_late = {
        (t.get("name"), c.get("name"))
        for t in tables
        for c in generate_columns_of(t)
        if isinstance(c, Mapping) and is_per_parent_aggregate(c)
    }
    problems: list[Problem | NamedProblem] = []
    for t_idx, table in enumerate(tables):
        columns = generate_columns_of(table)
        for c_idx, col in enumerate(columns):
            if not isinstance(col, Mapping):
                continue
            where: tuple[str | int, ...] = ("tables", t_idx, "generate_columns", c_idx)
            found = None
            if col.get("type") == "derived_aggregate":
                found = _aggregate_problem(col, table.get("name"), columns[:c_idx], by_name)
            option = _reads_aggregate(table, col, filled_late) if filled_late else None
            if found is not None:
                problems.append(_named(where, col, found))
            elif option is not None:
                problems.append(((*where, option), _READ_BEFORE_FILL))
    return problems


@dataclass(frozen=True)
class GenerateInputRef:
    """One resolved input: `option` of the consumer column reads the source column.

    Resolved from the registry's ``ref_kind`` descriptors. An empty ``source_table`` means the
    companion column leads to no table.
    """

    consumer_table: str
    consumer_column: str
    option: str
    source_table: str
    source_column: str
    must_be_earlier: bool
    phase: str


def _named_columns(kind: str, value: Any) -> list[str]:
    """The column names an option value holds; a derived expression that does not parse has none."""
    if kind == "column_list":
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
    if not isinstance(value, str):
        return []
    if kind != "expression":
        return [value]
    try:
        tree = compile_expr(value).tree
    except Exception:
        return []
    return [str(t.children[0]) for t in tree.iter_subtrees() if t.data == "col_ref"]


def _source_table(table: Mapping[str, Any], option: OptionSpec, context: Any) -> str | None:
    """The table an option's columns live in: "" when `context` leads to none, None to skip."""
    if option.ref_kind == "column_in_named_table":
        return context if isinstance(context, str) else None
    for sibling in generate_columns_of(table):
        if isinstance(sibling, Mapping) and sibling.get("name") == context:
            parent = sibling.get("reference_table")
            return parent if sibling.get("type") == "reference" and isinstance(parent, str) else ""
    return None


def iter_generate_input_refs(
    table: Mapping[str, Any], column: Mapping[str, Any]
) -> tuple[GenerateInputRef, ...]:
    """Every input of `column`, a generate column of `table`, resolved from the registry.

    The one resolver of ``ref_kind``. It reads the config and judges nothing: whether a source
    table or column exists and is ready is the plan check's rule alone. A missing companion
    column yields no edge for the option that needs it; the companion's own edge is refused.
    """
    spec = GENERATE_OPTIONS.get(str(column.get("type")))
    here = str(table.get("name"))
    refs: dict[GenerateInputRef, None] = {}
    for key, option in spec.keys.items() if spec is not None else ():
        if option.ref_kind is None:
            continue
        context = column.get(option.ref_context_key) if option.ref_context_key else None
        source, phase = here, option.ref_phase
        if option.ref_context_key and context is None:
            phase = "inline"
        elif option.ref_context_key:
            found = _source_table(table, option, context)
            if found is None:
                continue
            source = found
        earlier = phase == "inline" and source == here
        for name in _named_columns(option.ref_kind, column.get(key)):
            ref = GenerateInputRef(here, str(column.get("name")), key, source, name, earlier, phase)
            refs[ref] = None
    return tuple(refs)
