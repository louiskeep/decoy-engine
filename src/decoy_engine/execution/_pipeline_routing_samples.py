"""Per-table size specs and resident column samples for the routing estimates.

Split out of `_pipeline_routing_signals.py` (module-size budget), which re-exports both names;
they never read a lazy source (TB-1).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._mem_estimate_arrow import column_arrow_types as _column_arrow_types
from decoy_engine.execution._mem_estimate_arrow import source_nullable_columns
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from decoy_engine.profile._types import TableProfile


def _mask_specs(mask_tables: Sequence[Any], caller_sources: Mapping[str, Any]) -> tuple[Any, ...]:
    """The `TableSizeSpec` of each mask table, typed from its caller source if any."""
    from decoy_engine.execution._mem_estimate_schema import table_size_spec_from_profile

    return tuple(
        table_size_spec_from_profile(
            table,
            sample=_resident_column_arrays(caller_sources.get(table.name), table),
            arrow_types=_column_arrow_types(caller_sources.get(table.name), table),
            masked_columns=source_nullable_columns(caller_sources.get(table.name)),
        )
        for table in mask_tables
    )


def _resident_column_arrays(
    resident: pa.Table | LazySource | None, profile_table: TableProfile
) -> dict[str, pa.Array | pa.ChunkedArray]:
    """`{column_name: array}` for `profile_table`'s columns present in a
    RESIDENT source, or `{}` for a lazy (not-yet-loaded) table.

    Zero-copy: `pa.Table.column` returns a view into the existing Arrow
    buffer, not a copy.

    TB-1: a `LazySource` is treated like `None` -- sampling it would force a
    full read, which the byte-estimate / probe-recovery admission path must
    never do. An unsampleable variable-width column is UNPRICEABLE, which the
    byte-estimate router treats as "does not fit" and routes bounded.
    """
    if resident is None or isinstance(resident, LazySource):
        return {}
    names = set(resident.column_names)
    return {
        col.name: resident.column(col.name) for col in profile_table.columns if col.name in names
    }
