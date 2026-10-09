"""The output-schema rule `run_mask_chunked` applies on both routes.

Pandas infers a different Arrow type for degenerate chunks (an all-null string
column comes back `null`, a zero-row one `double`), so the oracle's per-chunk
types depend on chunk contents. For the strategies whose output type is fixed
by definition the rule pins one type per column for the whole call:

- hash, truncate and redact with a string `redact_with`, no `when:` predicate:
  `string`, reached by Arrow's checked cast so a value is never changed or lost.
- a native-admissible deterministic categorical column (all-string categories, a
  buildable CDF, a string source): `string`, by the same cast. Its all-string
  categories make string the intrinsic type; pandas' empty -> float64 and
  all-null -> null are inference artifacts that would otherwise vary with chunk
  boundaries. The full-frame route still resolves the type at assembly (all-null
  -> null), so this is a recorded route-dependent difference (see
  docs/compatibility-contract.md, ROUTE-OUTPUT-CONTRACT).
- a native-admissible date_shift column (explicit format, a namespace, no `group_by`, no
  `when:`, a string source): `string`, by the same cast. date_shift output is always a
  strftime string, so pandas' empty -> float64 and all-null -> null are inference
  artifacts. The decision reads only the config and the first chunk's type, never the
  companion, so both legs agree with or without it. The whole-frame route still resolves
  the type at assembly, so this is the same recorded route-dependent difference
  (docs/compatibility-contract.md, ROUTE-OUTPUT-CONTRACT).
- a native-admissible group_key column (no `when:`, a sibling the first chunk carries in
  `{string, int64, bool}` that no other node masks): `string`, by the same cast. Its output is
  always a populated hex string, so pandas' empty -> float64 is the only inference artifact
  (there is no all-null case). The decision keys on the SIBLING's type, never the target's,
  because the key overwrites the target. The whole-frame route still resolves an empty column
  to float64 at assembly, the same recorded route-dependent difference.
- a text_redact column whose config the native operator takes (no `ner`, a string `token`,
  list-or-null `detectors`, no `when:`): `string`, by the same cast. Its output is always
  strings or nulls, so pandas' empty -> float64 and all-null -> null are inference artifacts.
  A rejected config keeps the type its route produced: a non-string token or malformed
  detectors is a silent pass-through in the oracle (source column and type survive), and `ner`
  still masks but runs only on the oracle. Config only, so both legs agree.
  The whole-frame route still resolves an empty or all-null column to Arrow `null`, the same
  recorded route-dependent difference (docs/compatibility-contract.md, ROUTE-OUTPUT-CONTRACT).
- a faker column the chunked route admits as a position-keyed draw (non-deterministic REUSE, an
  explicit `pool_size`, a string-output provider, no `when:`): `string`, by the same cast. Its
  provider pool holds strings (a custom provider that returns anything else fails closed before
  any chunk), so pandas' empty -> float64 and all-null -> null are inference artifacts. Config
  only, so both legs agree. The whole-frame route still resolves an empty or all-null column to
  pandas' inferred type, the same recorded route-dependent difference
  (docs/compatibility-contract.md, ROUTE-OUTPUT-CONTRACT).
- a `when:` column the native route admits (hash, redact, truncate or deterministic categorical
  over a string source, closed-grammar predicate, see `_when_admission`): `string`, by the same
  cast. A column the predicate leaves untouched keeps its source value and type, so the output
  is always strings or nulls and pandas' empty -> null and all-null -> null are inference
  artifacts. Config plus the first chunk's source type, so both legs agree. The whole-frame
  route still resolves an empty or all-null column to Arrow `null`, the same recorded
  route-dependent difference (docs/compatibility-contract.md, ROUTE-OUTPUT-CONTRACT).
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

from decoy_engine.execution._column_access import handler_written_columns
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


def text_redact_pinned_columns(configured: dict[str, dict[str, Any]]) -> frozenset[str]:
    """The text_redact columns whose output is pinned to `string`.

    One classifier for both schema-rule construction sites. The pin follows the same config
    predicate admission uses, so a rejected config (an oracle pass-through, or `ner`, which
    masks only on the oracle) is never retyped."""
    from decoy_engine.execution.native._operator_config_rejections import (
        text_redact_config_rejection,
    )

    pinned: set[str] = set()
    for name, col in configured.items():
        if col.get("strategy") != "text_redact" or _has_when(col):
            continue
        provider_config = col.get("provider_config")
        cfg = provider_config if isinstance(provider_config, dict) else {}
        if text_redact_config_rejection(name, cfg) is None:
            pinned.add(name)
    return frozenset(pinned)


def faker_positional_pinned_columns(configured: dict[str, dict[str, Any]]) -> frozenset[str]:
    """The position-keyed faker columns whose output is pinned to `string`.

    One classifier for both schema-rule construction sites, config only: the pin follows stage A
    (`when:` is rejected for these columns, so a column carrying one is never pinned)."""
    from decoy_engine.execution.native._faker_positional_admission import (
        positional_faker_config_of_entry,
    )

    return frozenset(
        name
        for name, col in configured.items()
        if not _has_when(col) and positional_faker_config_of_entry(col) is not None
    )


def when_pinned_columns(
    config: dict[str, Any], first_schema: pa.Schema, *, table: str, registry: Any
) -> frozenset[str]:
    """The `when:` columns whose output is pinned to `string`.

    One classifier for both schema-rule construction sites: the native route's own admission
    verdict (`_when_admission`), taken from the config and the first chunk's real source
    type, independent of whether the compiled companion is installed."""
    from decoy_engine.execution.native._when_admission import admitted_when_columns

    return admitted_when_columns(config, registry, table=table, schema=first_schema)


def date_shift_pinned_columns(
    configured: dict[str, dict[str, Any]], first_schema: pa.Schema, *, table: str
) -> frozenset[str]:
    """The date_shift columns whose output is pinned to `string`.

    One classifier for both schema-rule construction sites (the dispatcher entry and the
    streamed output sink), config plus the first chunk's real source type, independent of
    whether the compiled companion is installed."""
    from decoy_engine.execution.native._operator_config_rejections import (
        date_shift_config_rejection,
    )

    pinned: set[str] = set()
    for name, col in configured.items():
        if col.get("strategy") != "date_shift" or _has_when(col):
            continue
        if name not in first_schema.names or first_schema.field(name).type != pa.string():
            continue
        provider_config = col.get("provider_config")
        reason = date_shift_config_rejection(
            name,
            table,
            None,
            namespace=col.get("namespace"),
            provider_config=provider_config if isinstance(provider_config, dict) else {},
        )
        if reason is None:
            pinned.add(name)
    return frozenset(pinned)


def group_key_pinned_columns(
    configured: dict[str, dict[str, Any]],
    first_schema: pa.Schema,
    *,
    table: str,
    written: frozenset[str] | None,
) -> frozenset[str]:
    """The group_key columns whose output is pinned to `string`.

    One classifier for both schema-rule construction sites, config plus the first chunk's real
    SIBLING type, independent of whether the compiled companion is installed. The sibling must
    reach group_key unmasked: not the column itself (a self-anchor), not a configured column
    with any strategy but a plain `passthrough`, and not a column another handler writes
    (`written`, the columns composites write beyond their own; `None` when unknown)."""
    from decoy_engine.execution.native._operator_config_rejections import (
        group_key_config_rejection,
        group_key_sibling_type_admitted,
    )

    pinned: set[str] = set()
    for name, col in configured.items():
        if col.get("strategy") != "group_key" or _has_when(col) or written is None:
            continue
        provider_config = col.get("provider_config")
        cfg = provider_config if isinstance(provider_config, dict) else {}
        group_by = cfg.get("group_by")
        if not isinstance(group_by, str) or group_by == name or group_by in written:
            continue
        sibling = configured.get(group_by)
        if sibling is not None and (sibling.get("strategy") != "passthrough" or _has_when(sibling)):
            continue
        if group_by not in first_schema.names:
            continue
        if not group_key_sibling_type_admitted(first_schema.field(group_by).type):
            continue
        if group_key_config_rejection(name, table, None, provider_config=cfg) is None:
            pinned.add(name)
    return frozenset(pinned)


@dataclass(frozen=True)
class SchemaRule:
    string_columns: frozenset[str]
    passthrough_types: dict[str, pa.DataType]
    # The first chunk's field (metadata and nullability included), reused for a
    # later null-typed chunk so both routes yield the same field on every chunk.
    passthrough_fields: dict[str, pa.Field]


def build_schema_rule(
    config: dict[str, Any],
    *,
    table: str,
    first: pa.Table,
    registry: Any,
    categorical_columns: frozenset[str] = frozenset(),
) -> SchemaRule:
    """Classify `table`'s columns once, from the config and the first chunk.

    `categorical_columns` are the native-admissible categorical columns (config-admissible
    over a string source, see `_categorical_prepared`): their output is `string` by
    definition, so they join the string-pinned set on both routes. A categorical column
    the native operator cannot run is not listed and keeps the type its route produced.

    A native-admissible date_shift column is added by `date_shift_pinned_columns`, a
    native-admissible group_key column by `group_key_pinned_columns`, a native-admissible
    text_redact column by `text_redact_pinned_columns`, a position-keyed faker column by
    `faker_positional_pinned_columns` and an admitted `when:` column by `when_pinned_columns`,
    so every caller gets the pins without passing anything.

    A column some handler writes (see `handler_written_columns`) is never a passthrough
    column here, so `normalize_chunk` cannot restore its source value."""
    table_cfg = next(
        (t for t in config.get("tables") or [] if isinstance(t, dict) and t.get("name") == table),
        {},
    )
    configured: dict[str, dict[str, Any]] = {
        c["name"]: c
        for c in table_cfg.get("columns") or []
        if isinstance(c, dict) and isinstance(c.get("name"), str)
    }
    written = handler_written_columns(
        [c for c in table_cfg.get("columns") or [] if isinstance(c, dict)], registry
    )
    from decoy_engine.execution._faker_degenerate_pin import deterministic_faker_pin_columns

    strings = frozenset(n for n, c in configured.items() if _string_output_is_fixed(c))
    strings |= categorical_columns
    strings |= text_redact_pinned_columns(configured)
    strings |= faker_positional_pinned_columns(configured)
    # C5c-ii: an admitted deterministic-Faker column over bool/int/uint pins its degenerate
    # output to `string` on both chunked legs, so the native leg (always `string`) and the
    # chunked oracle leg (pandas `null`/`double` for an all-null/empty chunk) agree.
    strings |= deterministic_faker_pin_columns(config, table, first.schema)
    strings |= date_shift_pinned_columns(configured, first.schema, table=table)
    strings |= when_pinned_columns(config, first.schema, table=table, registry=registry)
    strings |= group_key_pinned_columns(configured, first.schema, table=table, written=written)
    passthrough: dict[str, pa.DataType] = {}
    passthrough_fields: dict[str, pa.Field] = {}
    for field in first.schema:
        if written is None or field.name in written:
            continue
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
