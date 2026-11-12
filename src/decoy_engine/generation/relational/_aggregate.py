"""Per-parent aggregates: a parent column filled from its own children once every table exists.

A `derived_aggregate` with `from_table` and `via` (`config.is_per_parent_aggregate`) gives each
parent row `op` over the rows of the child table whose `via` foreign key points at it. The
column loop writes a placeholder (`aggregate_values`); `with_aggregates` runs after the
last table is drawn and returns the tables with each such parent table rebuilt from the filled
values. Validation refuses every input that would read the placeholder
(`config.generate_aggregate_read_before_fill`), so nothing drawn earlier depends on it.

Pattern: split-apply-combine (H. Wickham, "The Split-Apply-Combine Strategy for Data Analysis",
J. Stat. Softw. 40(1), 2011) with pandas `groupby`
(https://pandas.pydata.org/docs/reference/groupby.html), read as the SQL query
`SELECT p.key, op(c.column) FROM parent p LEFT JOIN child c ON c.via = p.key GROUP BY p.key`.
The groups are the child's non-null foreign keys (a null key joins no parent) and are
reindexed to the parent keys in row order. Each group's value is the scalar the same-table form
computes (`apply_derived_aggregate` over the closed `AGGREGATE_OPS`), so an op has one meaning
and null values are skipped. A parent with no children gets `COALESCE(op, 0)` for the additive
`sum` and `count` and a null for `mean`, `min` and `max`; a NaN from pandas becomes a null
before the column is written.

The inputs are the resolved edges of `config.iter_generate_input_refs`: the child table and
column of `column`, and the child's foreign key of `via`, whose `reference_column` is the parent
key. The filled column is typed by the one whole-table builder (`arrow_table`), so a declared
`output_type` applies as it does to any other column.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.config import (
    generate_columns_of,
    is_per_parent_aggregate,
    iter_generate_input_refs,
)
from decoy_engine.generation._statistical_column import arrow_table
from decoy_engine.transforms.derived_aggregate import (
    DerivedAggregateConfig,
    apply_derived_aggregate,
)

__all__ = ["aggregate_values", "with_aggregates"]

# The ops whose empty group is 0 rather than null: SQL COALESCE on the additive ops only.
_ADDITIVE = frozenset({"sum", "count"})


def aggregate_values(
    scalar: Callable[[dict[str, Any], int, dict[str, list[Any]]], list[Any]],
    col: dict[str, Any],
    n: int,
    generated: dict[str, list[Any]],
) -> list[Any]:
    """The column loop's values: nulls for a per-parent aggregate, else `scalar`'s.

    The caller passes its scalar generator so a same-table aggregate is drawn through the
    caller's own name for it, exactly as before.
    """
    if is_per_parent_aggregate(col):
        return [None] * n
    return scalar(col, n, generated)


def with_aggregates(config: Mapping[str, Any], tables: dict[str, pa.Table]) -> dict[str, pa.Table]:
    """`tables` with every per-parent aggregate column filled from its finished child table.

    Returns `tables` itself when the config has no per-parent aggregate. Otherwise a new
    mapping in which each parent table holding one is rebuilt; `tables` is not changed.
    """
    raw = config.get("tables")
    entries = [t for t in raw if isinstance(t, Mapping)] if isinstance(raw, list) else []
    pending = [
        (table, col)
        for table in entries
        for col in generate_columns_of(table)
        if isinstance(col, Mapping) and is_per_parent_aggregate(col)
    ]
    if not pending:
        return tables
    by_name = {t.get("name"): t for t in entries}
    out = dict(tables)
    for table, col in pending:
        name = str(table.get("name"))
        values = _per_parent_values(table, col, out, by_name)
        out[name] = _with_column(out[name], name, col, values)
    return out


def _per_parent_values(
    table: Mapping[str, Any],
    col: Mapping[str, Any],
    tables: Mapping[str, pa.Table],
    by_name: Mapping[Any, Mapping[str, Any]],
) -> list[Any]:
    """One aggregate per row of `table`, over the child rows whose foreign key names it."""
    edges = {edge.option: edge for edge in iter_generate_input_refs(table, col)}
    value, via = edges.get("column"), edges.get("via")
    child_config = by_name.get(via.source_table) if via is not None else None
    via_config = next(
        (
            c
            for c in generate_columns_of(child_config or {})
            if isinstance(c, Mapping) and via is not None and c.get("name") == via.source_column
        ),
        None,
    )
    key = via_config.get("reference_column") if via_config is not None else None
    parent = tables.get(str(table.get("name")))
    child = tables.get(via.source_table) if via is not None else None
    # Validation and the plan check refuse each of these; a caller that skipped both gets a
    # typed refusal that names the column only.
    if (
        value is None
        or via is None
        or value.source_table != via.source_table
        or child is None
        or parent is None
        or not isinstance(key, str)
        or key not in parent.column_names
        or value.source_column not in child.column_names
        or via.source_column not in child.column_names
    ):
        raise ValueError(
            f"generate column {table.get('name')!r}.{col.get('name')!r}: "
            "the per-parent aggregate's inputs do not resolve to a generated child table"
        )
    config = DerivedAggregateConfig.from_dict(
        {"op": col.get("op", ""), "column": value.source_column}
    )
    frame = pd.DataFrame(
        {
            "key": child.column(via.source_column).to_pylist(),
            "value": child.column(value.source_column).to_pylist(),
        },
        dtype=object,
    )
    groups = list(frame.groupby("key", sort=False, dropna=True)["value"])
    per_key = pd.Series(
        [apply_derived_aggregate(config, part) for _, part in groups],
        index=pd.Index([k for k, _ in groups], dtype=object),
        dtype=object,
    )
    parent_keys = pd.Index(parent.column(key).to_pylist(), dtype=object)
    filled = per_key.reindex(parent_keys).tolist()
    has_children = parent_keys.isin(per_key.index)
    empty = 0 if config.op in _ADDITIVE else None
    return [_cell(v) if here else empty for v, here in zip(filled, has_children, strict=True)]


def _cell(value: Any) -> Any:
    """A pandas missing value as None; anything else unchanged."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def _with_column(table: pa.Table, name: str, col: Mapping[str, Any], values: list[Any]) -> pa.Table:
    """`table` with the column `col` rebuilt from `values`, typed as the whole-table build types it."""
    column = str(col.get("name"))
    typed = arrow_table(
        {column: values}, table_name=name, statistical_specs={}, columns=[dict(col)]
    )
    index = table.schema.get_field_index(column)
    return table.set_column(index, typed.schema.field(0), typed.column(0))
