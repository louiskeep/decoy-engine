"""Per-chunk type guard for a positional draw under `when:` on the chunked route (C8-iii-d-2).

A seeded categorical or non-deterministic REUSE faker under `when:` runs chunked only when its
TARGET and every predicate REFERENCE is a chunk-stable `string`. The native leg converts and
evaluates each predicate reference per chunk, so a numeric reference can widen (int64 -> float64
around a null) and select different rows than the whole frame, which no position key can repair.
The config-time veto cannot see Arrow types; this guard runs where schemas exist.

Input batches are inferred independently, so a stream can start `string` and later supply a
numeric target or reference. The guard therefore checks the FIRST chunk (eagerly, in the shared
`_oracle_preflight`) AND every later chunk, before that chunk is masked. A later `null`-typed
chunk (an all-null batch the reader could not type) carries no widening hazard and is accepted.
Both chunked entry points run through `_oracle_preflight`, so installing it there covers the
native-dispatch route and the direct `run_mask_pipeline_chunked` oracle loop alike.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine.plan._errors import PlanCompileError

CAT_WHEN_CODE = "chunked_categorical_nondeterministic_when_not_supported"
FAKER_WHEN_CODE = "chunked_faker_nondeterministic_when_not_supported"


def closed_grammar_when(when: str) -> bool:
    """True when `when` parses in the closed predicate grammar (so its references are namable)."""
    from decoy_engine.errors import ValidationError
    from decoy_engine.expressions._when_parser import parse_when

    try:
        parse_when(when)
    except ValidationError:
        return False
    return True


@dataclass(frozen=True)
class _PositionalWhenColumn:
    column: str
    code: str
    # The target plus every predicate reference; each must stay `string`/`null` per chunk.
    names: tuple[str, ...]


@dataclass(frozen=True)
class PositionalWhenGuard:
    """The positional+`when:` columns of one table and the per-chunk string-type check."""

    columns: tuple[_PositionalWhenColumn, ...]

    def check_schema(self, schema: pa.Schema, *, table: str, chunk_index: int = 0) -> None:
        """Raise the strategy's code when a target or reference in this chunk is not
        `string`/`null`; `null` is an all-null chunk the reader could not type, which is safe."""
        for col in self.columns:
            for name in col.names:
                if name not in schema.names:
                    continue
                typ = schema.field(name).type
                if pa.types.is_string(typ) or pa.types.is_null(typ):
                    continue
                raise PlanCompileError(
                    code=col.code,
                    path=f"tables.{table}.columns",
                    message=(
                        f"column {col.column!r} runs a position-keyed draw under a 'when:' "
                        f"predicate, which the chunked route runs only when its target and every "
                        f"referenced column is a string; chunk {chunk_index} has {name!r} typed "
                        f"{typ}, whose per-chunk widening could select different rows than the "
                        "whole frame."
                    ),
                )

    def wrap(self, rest: Iterator[pa.Table], *, table: str) -> Iterator[pa.Table]:
        """`rest`, unchanged, raising the strategy's code at the first later chunk that drifts
        a target or reference off `string`/`null`, before that chunk is yielded."""
        for i, chunk in enumerate(rest, start=1):
            self.check_schema(chunk.schema, table=table, chunk_index=i)
            yield chunk


def plan_positional_when_guard(config: dict[str, Any], table: str) -> PositionalWhenGuard | None:
    """The table's positional+`when:` columns and what each must keep `string`, or None when the
    table has no such column. Classification is config-only (the same stage-A verdicts the route
    reads), so a non-positional or config-incomplete column is never guarded here."""
    from decoy_engine.execution.native._categorical_positional import positional_config_of_entry
    from decoy_engine.execution.native._faker_positional_admission import (
        positional_faker_config_of_entry,
    )
    from decoy_engine.expressions._when_parser import parse_when, when_column_refs

    table_cfg = next(
        (t for t in config.get("tables") or () if isinstance(t, dict) and t.get("name") == table),
        None,
    )
    if table_cfg is None:
        return None
    guarded: list[_PositionalWhenColumn] = []
    for entry in table_cfg.get("columns") or ():
        if not isinstance(entry, dict):
            continue
        when = entry.get("when")
        if not isinstance(when, str) or not when.strip() or not closed_grammar_when(when):
            continue
        if positional_config_of_entry(entry) is not None:
            code = CAT_WHEN_CODE
        elif positional_faker_config_of_entry(entry) is not None:
            code = FAKER_WHEN_CODE
        else:
            continue
        column = str(entry.get("name", "?"))
        refs = when_column_refs(parse_when(when))
        names = (column, *(r for r in refs if r != column))
        guarded.append(_PositionalWhenColumn(column, code, names))
    return PositionalWhenGuard(tuple(guarded)) if guarded else None


__all__ = [
    "CAT_WHEN_CODE",
    "FAKER_WHEN_CODE",
    "PositionalWhenGuard",
    "closed_grammar_when",
    "plan_positional_when_guard",
]
