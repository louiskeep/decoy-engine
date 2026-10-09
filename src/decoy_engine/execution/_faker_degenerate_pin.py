"""C5c-ii: pin an admitted deterministic-Faker column's degenerate output to Arrow `string`.

A deterministic Faker column over a bool/int/uint source draws from a string-output provider, so
a value-bearing output is always `string`. Its DEGENERATE output (all-null or empty) would be the
type pandas infers at the Arrow boundary instead: Arrow `null` for all-null, `double` for empty.
Decision (Cam 2026-10-09, option A): pin that degenerate output to `string` on every route, as
positional Faker and C1 categorical already do on the chunked legs, since the string-output
provider makes `string` the honest type and `double`/`null` are empty/all-null inference artifacts.

The pin set is a CONFIG + SOURCE-FAMILY predicate, independent of native admission or the chunked
stream guarantee: an otherwise pin-eligible column that declines native acceleration (no producer
guarantee) is still pinned, so both routes agree. A `when:` predicate can leave value-bearing
cells, so a `when:`-bearing column is never pinned. An FK key column the join resolution overrides
is never pinned either: the Faker pool does not own that column's output.

`pin_degenerate_to_string` is applied AFTER `pa.Table.from_pandas` on the full-frame oracle and
unified routes (a cast alone leaves pandas' `pandas_type` float64/empty, so it also rewrites the
`b"pandas"` metadata entry to unicode/object). The chunked route reaches the same result through
`_chunked_schema_rule`'s `string_columns` cast, which carries no pandas metadata by contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pyarrow as pa

from decoy_engine.execution.native._faker_deterministic_admission import (
    DETERMINISTIC_FAKER_SOURCE_TYPES,
    is_effective_deterministic_faker,
)

__all__ = [
    "deterministic_faker_pin_columns",
    "deterministic_faker_pin_columns_from_plan",
    "pin_degenerate_to_string",
    "pin_frame_outputs",
]


def _has_when(col: Mapping[str, Any]) -> bool:
    when = col.get("when")
    return isinstance(when, str) and bool(when.strip())


def _fk_child_columns(config: Mapping[str, Any], table: str) -> set[str]:
    """Every column of `table` declared as an FK child key in `config["relationships"]`; the
    join resolution writes these, so the Faker pool does not own their output."""
    out: set[str] = set()
    for rel in config.get("relationships") or ():
        if not isinstance(rel, Mapping):
            continue
        for child in rel.get("children") or ():
            if isinstance(child, Mapping) and child.get("table") == table:
                out.update(str(c) for c in child.get("columns") or [])
    return out


def deterministic_faker_pin_columns(
    config: Mapping[str, Any], table: str, source_schema: pa.Schema
) -> frozenset[str]:
    """The columns of `table` whose degenerate deterministic-Faker output is pinned to `string`.

    Effective-deterministic Faker over a bool/int/uint source, not `when:`-bearing and not an FK
    child key. The string-output provider / pool content is validated by the route's own pool
    check, not here."""
    table_cfg = next(
        (
            t
            for t in config.get("tables") or []
            if isinstance(t, Mapping) and t.get("name") == table
        ),
        None,
    )
    if table_cfg is None:
        return frozenset()
    fk_children = _fk_child_columns(config, table)
    source_names = set(source_schema.names)
    pinned: set[str] = set()
    for col in table_cfg.get("columns") or []:
        if not isinstance(col, Mapping):
            continue
        name = col.get("name")
        if not isinstance(name, str) or name not in source_names or name in fk_children:
            continue
        if _has_when(col) or not is_effective_deterministic_faker(col):
            continue
        if source_schema.field(name).type in DETERMINISTIC_FAKER_SOURCE_TYPES:
            pinned.add(name)
    return frozenset(pinned)


def deterministic_faker_pin_columns_from_plan(
    plan: Any, table: str, source_schema: pa.Schema, *, relationship_graph: Any = None
) -> frozenset[str]:
    """`deterministic_faker_pin_columns` from the compiled plan's column seeds, for the full-frame
    oracle routes that work from the plan rather than the raw config. The `allow_collisions` alias
    is already folded into `seed.deterministic` by the seed envelope; a `when:`-bearing seed and an
    FK child key (from `relationship_graph`) are excluded, matching the config-based predicate."""
    table_seed = next((ts for (name, ts) in plan.seed_envelope.per_table if name == table), None)
    if table_seed is None:
        return frozenset()
    fk_children: set[str] = set()
    for edge in getattr(relationship_graph, "edges", ()) or ():
        if edge.child_table == table:
            fk_children.update(edge.child_columns)
    source_names = set(source_schema.names)
    pinned: set[str] = set()
    for name, seed in table_seed.per_column:
        if name not in source_names or name in fk_children:
            continue
        if seed.strategy != "faker" or not seed.deterministic or seed.when:
            continue
        if source_schema.field(name).type in DETERMINISTIC_FAKER_SOURCE_TYPES:
            pinned.add(name)
    return frozenset(pinned)


def pin_frame_outputs(
    outputs: Mapping[str, pa.Table],
    plan: Any,
    sources: Mapping[str, pa.Table],
    *,
    relationship_graph: Any = None,
) -> dict[str, pa.Table]:
    """Apply the degenerate pin to each from_pandas output using the plan and the pre-mask source
    schema. A table absent from `sources` (a generate echo) is returned unchanged."""
    return {
        t: pin_degenerate_to_string(
            out,
            deterministic_faker_pin_columns_from_plan(
                plan, t, sources[t].schema, relationship_graph=relationship_graph
            )
            if t in sources
            else frozenset(),
        )
        for t, out in outputs.items()
    }


def _patched_pandas_metadata(
    metadata: Mapping[bytes, bytes] | None, names: set[str]
) -> dict[bytes, bytes] | None:
    """Rewrite the `b"pandas"` schema-metadata entry of each column in `names` to the string
    shape (`pandas_type: unicode`, `numpy_type: object`), leaving all other metadata untouched.
    No-op when there is no pandas metadata (the chunked route)."""
    if not metadata or b"pandas" not in metadata:
        return dict(metadata) if metadata else None
    meta = dict(metadata)
    # from_pandas emits well-formed JSON here; a parse failure would be a pyarrow regression.
    obj = json.loads(meta[b"pandas"])
    for entry in obj.get("columns", []):
        if isinstance(entry, dict) and entry.get("name") in names:
            entry["pandas_type"] = "unicode"
            entry["numpy_type"] = "object"
    meta[b"pandas"] = json.dumps(obj).encode("utf-8")
    return meta


def pin_degenerate_to_string(table: pa.Table, columns: frozenset[str]) -> pa.Table:
    """Retype each column in `columns` whose output is all-null or empty to Arrow `string`,
    rewriting its `b"pandas"` metadata to the string shape. Value-bearing columns (already
    `string`) and unrelated columns are untouched; an empty `columns` set is a no-op."""
    if not columns:
        return table
    degenerate: list[int] = []
    for i, field in enumerate(table.schema):
        if field.name not in columns or field.type == pa.string():
            continue
        if table.num_rows == 0 or table.column(i).null_count == table.num_rows:
            degenerate.append(i)
    if not degenerate:
        return table
    pinned_names: set[str] = set()
    for i in degenerate:
        field = table.schema.field(i)
        pinned_names.add(field.name)
        column = table.column(i).cast(pa.string())
        table = table.set_column(
            i, pa.field(field.name, pa.string(), nullable=field.nullable), column
        )
    return table.replace_schema_metadata(
        _patched_pandas_metadata(table.schema.metadata, pinned_names)
    )
