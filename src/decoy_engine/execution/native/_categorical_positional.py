"""Chunked admission of the seeded non-deterministic categorical: node and entry adapters.

Stage A (config only) lives in `_categorical_prepared.prepare_positional_categorical`; this
module reads it from the two shapes a consumer holds before any chunk: a raw column entry
(the compatibility veto) and a config + table + column name (the static route decision and
the evidence planner). Stage B (the string source) is checked here once the first chunk's
schema is known, and a failure is a refusal, not a reroute: sending the column to the
chunked oracle would open a second route the plan did not admit.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pyarrow as pa

from decoy_engine.execution.native._categorical_prepared import (
    PreparedCategorical,
    prepare_positional_categorical,
    source_is_string,
)
from decoy_engine.execution.native._operator_config_rejections import (
    is_deterministic_categorical,
)
from decoy_engine.plan._errors import PlanCompileError

NONDETERMINISTIC_CODE = "categorical_nondeterministic_not_chunk_safe"


def positional_config_of_entry(col_entry: dict[str, Any]) -> PreparedCategorical | None:
    """The stage-A artifact for a raw column entry, or None when it is not the seeded
    non-deterministic categorical or its config fails stage A."""
    if col_entry.get("strategy") != "categorical" or is_deterministic_categorical(col_entry):
        return None
    artifact, _reason = prepare_positional_categorical(
        str(col_entry.get("name", "?")),
        namespace=col_entry.get("namespace"),
        provider_config=dict(col_entry.get("provider_config") or {}),
    )
    return artifact


def positional_config_for_column(
    config: dict[str, Any], table: str, column: str
) -> PreparedCategorical | None:
    """`positional_config_of_entry` for `column` of `table` in a whole job config."""
    for table_cfg in config.get("tables") or ():
        if not isinstance(table_cfg, dict) or table_cfg.get("name") != table:
            continue
        for col in table_cfg.get("columns") or ():
            if isinstance(col, dict) and col.get("name") == column:
                return positional_config_of_entry(col)
    return None


def reject_non_string_positional_sources(
    config: dict[str, Any], node_routes: Iterable[Any], first_schema: pa.Schema, *, table: str
) -> None:
    """Stage B: refuse a stage-A-admissible seeded categorical whose real source is not
    `string`, with the retained categorical chunked code. The deterministic variant keeps
    its oracle reroute (`real_type_rejection`); only this variant fails closed."""
    for node in node_routes:
        if node.strategy != "categorical":
            continue
        if positional_config_for_column(config, table, node.column) is None:
            continue
        if not source_is_string(first_schema, node.column):
            typ = first_schema.field(node.column).type
            raise PlanCompileError(
                code=NONDETERMINISTIC_CODE,
                path=f"tables.{table}.columns",
                message=(
                    f"non-deterministic categorical column {node.column!r} has a {typ} source; "
                    "the chunked route runs it only over a string source and does not fall "
                    "back to the oracle. Cast the source to string, or set `deterministic: "
                    "true` with a namespace."
                ),
            )
