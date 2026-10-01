"""The output-schema rule `run_mask_chunked` applies on both routes.

Pandas infers a different Arrow type for degenerate chunks (an all-null string
column comes back `null`, a zero-row one `double`), so the oracle's per-chunk
types depend on chunk contents. For the strategies whose output type is fixed
by definition the rule pins one type per column for the whole call:

- hash, truncate and redact with a string `redact_with`, no `when:` predicate:
  `string`, reached by Arrow's checked cast so a value is never changed or lost.
- passthrough columns (configured, or unconfigured and kept under the
  passthrough policy): the source column itself, never the pandas round trip,
  which rounds nullable integers above 2^53.

Everything else keeps the type its route produced. No chunk carries pandas
schema metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._errors import ExecutionError

_STRING_OUTPUT_STRATEGIES = frozenset({"hash", "truncate", "redact"})


def _has_when(col: dict[str, Any]) -> bool:
    # Same normalization the plan compiler uses: a blank predicate has no effect.
    when = col.get("when")
    return isinstance(when, str) and bool(when.strip())


def _string_output_is_fixed(col: dict[str, Any]) -> bool:
    if col.get("strategy") not in _STRING_OUTPUT_STRATEGIES or _has_when(col):
        return False
    if col["strategy"] != "redact":
        return True
    cfg = col.get("provider_config") or {}
    return isinstance(cfg.get("redact_with", "REDACTED"), str)


@dataclass(frozen=True)
class SchemaRule:
    string_columns: frozenset[str]
    passthrough_types: dict[str, pa.DataType]
    # The first chunk's field (metadata and nullability included), reused for a
    # later null-typed chunk so both routes yield the same field on every chunk.
    passthrough_fields: dict[str, pa.Field]


def build_schema_rule(config: dict[str, Any], *, table: str, first: pa.Table) -> SchemaRule:
    """Classify `table`'s columns once, from the config and the first chunk."""
    table_cfg = next(
        (t for t in config.get("tables") or [] if isinstance(t, dict) and t.get("name") == table),
        {},
    )
    configured: dict[str, dict[str, Any]] = {
        c["name"]: c
        for c in table_cfg.get("columns") or []
        if isinstance(c, dict) and isinstance(c.get("name"), str)
    }
    strings = frozenset(n for n, c in configured.items() if _string_output_is_fixed(c))
    passthrough: dict[str, pa.DataType] = {}
    passthrough_fields: dict[str, pa.Field] = {}
    for field in first.schema:
        col = configured.get(field.name)
        if col is None or (col.get("strategy") == "passthrough" and not _has_when(col)):
            passthrough[field.name] = field.type
            passthrough_fields[field.name] = field
    return SchemaRule(
        string_columns=strings, passthrough_types=passthrough, passthrough_fields=passthrough_fields
    )


def _safe_cast(column: Any, target: pa.DataType, *, table: str, name: str, chunk_index: int) -> Any:
    try:
        return pc.cast(column, target, safe=True)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
        raise ExecutionError(
            code="chunked_schema_mismatch",
            message=(
                f"{table!r} chunk {chunk_index}: column {name!r} came back as "
                f"{column.type} but every chunk of this call must be {target}, and the "
                f"values cannot be converted without loss ({exc})"
            ),
        ) from exc


def normalize_chunk(
    rule: SchemaRule, produced: pa.Table, source: pa.Table, *, table: str, chunk_index: int
) -> pa.Table:
    """Return `produced` under the rule, without schema metadata."""
    arrays: list[Any] = []
    fields: list[pa.Field] = []
    for i, field in enumerate(produced.schema):
        column = produced.column(i)
        name = field.name
        if name in rule.passthrough_types and name in source.column_names:
            # Source drift was refused upstream, so the source column has the
            # declared type or is `null`-typed (a later all-null chunk), which is
            # brought to the declared type here, after any masking ran.
            column = source.column(name)
            declared = rule.passthrough_types[name]
            if column.type != declared:
                column = column.cast(declared)
            field = rule.passthrough_fields[name]
        elif name in rule.string_columns and column.type != pa.string():
            column = _safe_cast(
                column, pa.string(), table=table, name=name, chunk_index=chunk_index
            )
            field = pa.field(name, pa.string(), nullable=field.nullable)
        arrays.append(column)
        fields.append(field)
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))
