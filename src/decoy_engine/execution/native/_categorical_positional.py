"""Chunked admission of the seeded non-deterministic categorical: node and entry adapters.

Stage A (config only) lives in `_categorical_prepared.prepare_positional_categorical`; this
module reads it from the two shapes a consumer holds before any chunk: a raw column entry
(the compatibility veto) and a config + table + column name (the static route decision and
the evidence planner). Stage B (the source dtype) is leg selection, not admission: a
string source runs the native kernel, and any other type reroutes to the chunked oracle
through `real_type_rejection`, the same path the deterministic categorical takes.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.execution.native._categorical_prepared import (
    PreparedCategorical,
    prepare_positional_categorical,
)
from decoy_engine.execution.native._operator_config_rejections import (
    is_deterministic_categorical,
)


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
