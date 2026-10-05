"""Chunked-route admission checks that only group_key needs.

group_key keys each row on a SIBLING column of the same row, so a chunk always holds the
value it needs and chunking cannot split it. Two properties still have to hold for the native
chunked leg to equal the oracle chunked leg, and both are decided here, from the config alone.

- The sibling must reach group_key unmasked. The oracle reads the `group_by` column at
  group_key's position in the work order, so another node that masks the sibling first changes
  what it reads. The native leg masks every column from the source chunk and never feeds a
  masked column back, so a masked or self-anchored sibling stays on the oracle leg. This is the
  gate the full-frame route applies (`_unified_slice_admission._group_key_sibling_admitted`);
  that one answers a boolean, so the chunked route names its own reasons.
- The sibling's Arrow type must be in the collision-free native domain. That is the real-type
  check in `_real_type_admission`, not this module, because it needs the first chunk.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pyarrow as pa


def group_by_columns(config: dict[str, Any], table: str) -> dict[str, str]:
    """`{group_key column: its group_by column}` for `table`, read from the config.

    A column whose `group_by` is not a non-empty string is left out: the plan compiler
    rejects it before routing."""
    out: dict[str, str] = {}
    for table_cfg in config.get("tables") or ():
        if not isinstance(table_cfg, dict) or table_cfg.get("name") != table:
            continue
        for col in table_cfg.get("columns") or ():
            if not isinstance(col, dict) or col.get("strategy") != "group_key":
                continue
            provider_config = col.get("provider_config")
            group_by = (
                provider_config.get("group_by") if isinstance(provider_config, dict) else None
            )
            if isinstance(col.get("name"), str) and isinstance(group_by, str) and group_by:
                out[col["name"]] = group_by
    return out


def sibling_resident_sources(
    config: dict[str, Any], table: str, first_schema: pa.Schema | None
) -> dict[str, pa.Table] | None:
    """The first chunk's group_by siblings as an empty resident table, or None when there are
    none to give.

    The plan compiler reads a sibling's type from the profile, whose coarse labels call an
    int64 column that holds nulls `double`, so the compiled node would reject a sibling the
    native leg runs. Giving the compiler the real first-chunk type, for the siblings only,
    makes that one decision resident-Arrow-authoritative, as the full-frame route's is, and
    leaves every other column on the profile."""
    if first_schema is None:
        return None
    fields: dict[str, pa.Field] = {}
    for group_by in group_by_columns(config, table).values():
        if group_by in first_schema.names:
            fields[group_by] = first_schema.field(group_by)
    if not fields:
        return None
    try:
        return {table: pa.schema(list(fields.values())).empty_table()}
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
        # An exotic sibling type (a union) cannot build an empty table; the real-type gate
        # still declines it.
        return None


def order_dependence_rejection(
    column: str, group_by: str, table_nodes: Iterable[Any]
) -> str | None:
    """The coded reason a group_key column's sibling is not an unmasked passthrough, or None.

    `table_nodes` are the compiled plan nodes of the table. Any node other than a passthrough
    that covers the sibling masks it, and a self-anchor (`group_by` is the column itself) is
    such a node by definition. Composite nodes count: they cover several columns."""
    if group_by == column:
        return f"group_key_self_anchor_not_native_chunked_route:{column}"
    for node in table_nodes:
        if group_by in node.columns and node.strategy != "passthrough":
            return f"group_key_masked_sibling_not_native_chunked_route:{column}:{group_by}"
    return None
