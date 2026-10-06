"""Which `when:` columns the chunked native route can run, from config and the first chunk.

One verdict (`when_native_rejection`) for every consumer: the route decision
(`_dispatch.plan_native_route`), the output-type pin (`_chunked_schema_rule`) and the
auto-chunk planner. The row mask itself comes from the oracle's own predicate function
(`_when_mask`), so any predicate in the closed grammar qualifies; what this module decides
is whether the COLUMN around it is one the masked kernel step reproduces exactly:

1. the strategy is hash, redact, truncate or deterministic categorical, and its own config
   passes the same config-only gate the native route already applies;
2. the target source type is `string`, because the masked step selects between two string
   arrays;
3. the predicate parses under the closed grammar (a raw-dict predicate outside it declines);
4. every column the predicate reads is the target itself or one no EARLIER work node writes,
   because the oracle evaluates the predicate on the live frame, after earlier masks.

Rule 1 and 2 keep the long-standing `when_predicate_not_native:<column>` code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa

from decoy_engine.execution._column_access import column_access, has_when
from decoy_engine.expressions._when_parser import (
    WhenExpr,
    parse_when,
    when_column_refs,
)

NOT_NATIVE_CODE = "when_predicate_not_native"
OUTSIDE_SUBSET_CODE = "when_predicate_outside_native_subset"
READS_MASKED_CODE = "when_predicate_reads_masked_column"

ADMITTED_WHEN_STRATEGIES = frozenset({"hash", "redact", "truncate", "categorical"})


def table_column_entries(config: Mapping[str, Any], table: str) -> list[Mapping[str, Any]]:
    """The dict column entries of `table` in the raw config, in config order."""
    for table_cfg in config.get("tables") or ():
        if isinstance(table_cfg, dict) and table_cfg.get("name") == table:
            return [c for c in table_cfg.get("columns") or () if isinstance(c, dict)]
    return []


def parsed_when(entry: Mapping[str, Any]) -> WhenExpr | None:
    """The entry's predicate under the closed grammar, or None when it is outside it."""
    from decoy_engine.errors import ValidationError

    try:
        return parse_when(str(entry.get("when")))
    except ValidationError:
        return None


def _node_key(entry: Mapping[str, Any], registry: Any) -> tuple[str, ...]:
    """The work node's sort key (`order_work` orders by `(table, columns)`, and the chunked
    route has no relationship edges, so this order is the execution order)."""
    from decoy_engine.execution._runner import provider_is_composite

    name = str(entry.get("name", ""))
    if provider_is_composite(entry.get("provider"), registry):
        coherent = entry.get("coherent_with")
        members = (
            {name, *(c for c in coherent if isinstance(c, str))}
            if isinstance(coherent, list | tuple)
            else {name}
        )
        return tuple(sorted(members))
    return (name,)


def _earlier_writes(
    target: str, entries: Sequence[Mapping[str, Any]], registry: Any
) -> frozenset[str] | None:
    """Columns written by work nodes that run before `target`'s node, or None when some
    such node's writes cannot be named.

    A node's writes are its own target column plus its declared extra writes: the
    declaration helper reports only the extras for a scalar node. A passthrough node
    writes a value equal to the source, so it is not a write. A node with unknown reads
    also yields None: it cannot change a value, so declining after it is conservative
    policy rather than a parity requirement.
    """
    target_entry = next((e for e in entries if e.get("name") == target), None)
    if target_entry is None:
        return None
    target_key = _node_key(target_entry, registry)
    written: set[str] = set()
    for entry in entries:
        name = entry.get("name")
        if entry is target_entry or not isinstance(name, str):
            continue
        if _node_key(entry, registry) >= target_key:
            continue
        access = column_access(entry, registry)
        if access.writes_unknown or access.reads_unknown:
            return None
        written |= access.writes
        if entry.get("strategy") != "passthrough":
            written.add(name)
    return frozenset(written)


def when_native_rejection(
    column: str,
    entries: Sequence[Mapping[str, Any]],
    registry: Any,
    *,
    table: str,
    schema: pa.Schema | None,
) -> str | None:
    """The coded reason `column`'s `when:` cannot run natively, or None when it can.

    `schema` is the first chunk's (or the planner's source) schema; None means the target
    type is unknown, which declines.
    """
    from decoy_engine.execution.native._plan import _column_rejection

    if registry is None:
        from decoy_engine.providers_v2 import get_default_registry

        registry = get_default_registry()
    entry = next((e for e in entries if e.get("name") == column), None)
    if entry is None or not has_when(entry):
        return f"{NOT_NATIVE_CODE}:{column}"
    if (
        entry.get("strategy") not in ADMITTED_WHEN_STRATEGIES
        or _column_rejection(dict(entry), registry, table=table, profile=None) is not None
    ):
        return f"{NOT_NATIVE_CODE}:{column}"
    if schema is None or column not in schema.names or schema.field(column).type != pa.string():
        return f"{NOT_NATIVE_CODE}:{column}"
    ast = parsed_when(entry)
    if ast is None:
        return f"{OUTSIDE_SUBSET_CODE}:{column}"
    refs = when_column_refs(ast)
    written = _earlier_writes(column, entries, registry)
    if written is None:
        return f"{READS_MASKED_CODE}:{column}:{refs[0]}"
    for ref in refs:
        if ref != column and ref in written:
            return f"{READS_MASKED_CODE}:{column}:{ref}"
    return None


def first_when_rejection(
    config: Mapping[str, Any],
    registry: Any,
    *,
    table: str,
    schema: pa.Schema | None,
) -> str | None:
    """The first config-order `when:` column of `table` that cannot run natively, as its
    code, or None when the table has no `when:` or every one of them can."""
    entries = table_column_entries(config, table)
    for entry in entries:
        if has_when(entry):
            reason = when_native_rejection(
                str(entry.get("name", "?")), entries, registry, table=table, schema=schema
            )
            if reason is not None:
                return reason
    return None


def admitted_when_columns(
    config: Mapping[str, Any],
    registry: Any,
    *,
    table: str,
    schema: pa.Schema | None,
) -> frozenset[str]:
    """The `when:` columns of `table` that pass `when_native_rejection`."""
    entries = table_column_entries(config, table)
    return frozenset(
        str(e["name"])
        for e in entries
        if has_when(e)
        and isinstance(e.get("name"), str)
        and when_native_rejection(e["name"], entries, registry, table=table, schema=schema) is None
    )


def planner_relaxed_when_columns(
    config: Mapping[str, Any],
    registry: Any,
    table: str,
    source_tables: Mapping[str, Any] | None,
    source_facts: dict[str, Any],
) -> frozenset[str]:
    """The `when:` columns the auto-chunk planner may chunk: admitted natively (rules 1, 3 and 4)
    with the target and EVERY referenced column `string` in `schema`.

    Auto-chunking must equal the WHOLE-FRAME run, not only the chunked oracle, and a numeric
    reference can widen int64 to float64 in some chunks and not others, so per-chunk and
    whole-frame masks can differ above 2**53. String references have one representation.
    A static classification (no loaded source) has no schema and relaxes nothing.
    """
    from decoy_engine.execution._chunked_input import facts_for

    src = (source_tables or {}).get(table)
    if src is None:
        return frozenset()
    schema = facts_for(source_facts, src, table).schema
    entries = table_column_entries(config, table)
    relaxed: set[str] = set()
    for name in admitted_when_columns(config, registry, table=table, schema=schema):
        entry = next(e for e in entries if e.get("name") == name)
        ast = parsed_when(entry)
        refs = () if ast is None else when_column_refs(ast)
        if refs and all(r in schema.names and schema.field(r).type == pa.string() for r in refs):
            relaxed.add(name)
    return frozenset(relaxed)


__all__ = [
    "ADMITTED_WHEN_STRATEGIES",
    "NOT_NATIVE_CODE",
    "OUTSIDE_SUBSET_CODE",
    "READS_MASKED_CODE",
    "admitted_when_columns",
    "first_when_rejection",
    "parsed_when",
    "planner_relaxed_when_columns",
    "table_column_entries",
    "when_native_rejection",
]
