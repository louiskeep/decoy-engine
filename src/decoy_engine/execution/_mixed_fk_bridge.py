"""Whole-table FK key resolution for a mask child whose parent is a GENERATE
table (the one edge a coupled mixed job admits).

The oracle's pool read for this shape: a generate parent is never masked, so
its pandas source->masked map is the IDENTITY map (`_pandas_adapter.py::
_parent_map`, `src_series` falls back to the never-mutated frame). This module
reproduces that identity map from the generate parent's materialized Arrow
output directly (the round-trip-stability gate in `_mixed_admission` already
proves the pandas echo is the identity for every admitted generate shape), then
resolves the child's FK column through the SAME shared `resolve_fk_keys` policy
engine the oracle uses (`execution._fk_resolve`), so the map-hit / orphan /
REMAP precedence cannot drift between the oracle, the shadow and the
production mixed route. Both of those import this module; it sits at the
`execution` parent level so neither depends on the other.

Byte parity for the RESOLVED CHILD column needs one more step than the map: the
oracle writes a resolved FK column back through the lossless pandas<->Arrow
boundary (`_fk_keys.py`'s `to_pandas_fk_safe` ingestion and
`lossless_fk_int_values` / `fk_nullable_int_array` / `fk_all_null_array`
write-back), not a bare list assignment. A null-bearing integer column built
the naive way silently widens to float64 and rounds any key past 2**53, so
`_write_back_fk_column` reproduces that exact bridge over a one-column frame.

Scope: single-column (non-composite) int/string keys. A REMAP orphan against a
generate parent always fails identically to the oracle's
`orphan_remap_parent_missing` (a generate table is never a masked `WorkNode`, so
`make_remap_fn`'s parent lookup always misses for this shape); `_reject_remap`
raises the identical coded error rather than importing the pandas-bound
closure.

Because the production route resolves the key over the WHOLE child column in
one call, a WARN orphan policy yields exactly one edge-level `orphan_fk`
warning whose count is the whole-column count: the same shape the oracle
emits, with no cross-chunk summing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import (
    fk_all_null_array,
    fk_key_value,
    fk_nullable_int_array,
    lossless_fk_int_values,
    to_pandas_fk_safe,
)
from decoy_engine.execution._fk_resolve import resolve_fk_keys

if TYPE_CHECKING:
    from collections.abc import Callable

    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.relationships._graph import RelationshipEdge

_KeyTuple = tuple[object, ...]

__all__ = ["FkResolution", "resolve_generate_parent_fk_child"]


@dataclass(frozen=True)
class FkResolution:
    """One admitted FK child's resolved key column plus any WARN diagnostics the
    orphan policy emitted (aggregated, one per edge -- see
    `execution._fk_resolve.resolve_fk_keys`)."""

    column: pa.Array
    warnings: tuple[QualityWarning, ...]


def resolve_generate_parent_fk_child(
    edge: RelationshipEdge, *, parent_table: pa.Table, child_table: pa.Table
) -> FkResolution:
    """Resolve one admitted single-column generate-parent -> mask-child FK edge:
    build the parent's identity map, run it through the shared orphan-policy
    engine, and write the resolved column back through the oracle's own lossless
    pandas<->Arrow boundary. `child_table` needs only the child key column."""
    parent_column = edge.parent_columns[0]
    child_column = edge.child_columns[0]
    parent_map = _identity_parent_map(parent_table, parent_column)
    child_keys = _child_key_tuples(child_table, child_column)
    masked_keys, warnings, _cascade = resolve_fk_keys(
        child_keys,
        parent_map,
        edge,
        remap_fn=_reject_remap(edge),
        # A generate parent is never row-errored (quarantine is excluded from the
        # mixed shape), so there is nothing to cascade.
        errored_parent_keys=None,
    )
    values: list[object] = [None if key is None else key[0] for key in masked_keys]
    column = _write_back_fk_column(child_table, child_column, values)
    return FkResolution(column=column, warnings=tuple(warnings))


def _identity_parent_map(parent_table: pa.Table, parent_column: str) -> dict[_KeyTuple, _KeyTuple]:
    """The generate parent's source-key -> masked-key map, reproduced as an
    IDENTITY map with the oracle's exact normalization: a parent row with a null
    key contributes nothing (an unreferenced key, matching the oracle's own
    null-key skip); every other row's key normalizes via the shared
    `fk_key_value` for the map LOOKUP, while the mapped VALUE preserves the
    parent's own original value, unnormalized (an orphan-preserved or matched
    child must read back the exact source type/value). Row order iterates the
    table's own order, so a duplicate key's LAST row wins, the same
    last-write-wins a plain Python dict assignment gives the oracle's loop.
    """
    out: dict[_KeyTuple, _KeyTuple] = {}
    for value in parent_table.column(parent_column).to_pylist():
        if value is None:
            continue
        out[(fk_key_value(value),)] = (value,)
    return out


def _child_key_tuples(child_table: pa.Table, child_column: str) -> list[_KeyTuple | None]:
    """One key tuple per child row (`None` for a null FK, preserved as null and
    never an orphan -- matches `_pandas_adapter.py::_resolve_fk_node`). Arrow's
    own `to_pylist()` already returns exact Python ints for an int64-with-nulls
    column (no float64-on-null promotion), so no FK-safe pandas ingestion is
    needed on this READ side; only the write-back needs the lossless bridge."""
    return [
        None if value is None else (fk_key_value(value),)
        for value in child_table.column(child_column).to_pylist()
    ]


def _reject_remap(edge: RelationshipEdge) -> Callable[[list[_KeyTuple]], list[_KeyTuple]]:
    """A REMAP orphan against a generate parent always fails: `make_remap_fn`
    mints a remapped key via the PARENT column's masked `WorkNode`, and a
    generate-kind table never has one. The oracle's own closure raises
    `orphan_remap_parent_missing` for this shape every time; this closure raises
    the identical coded exception with the identical message."""
    parent_table = edge.parent_table
    parent_column = edge.parent_columns[0]

    def _remap(orphan_keys: list[_KeyTuple]) -> list[_KeyTuple]:
        del orphan_keys  # unused: this edge shape always rejects, never remaps
        raise ExecutionError(
            code="orphan_remap_parent_missing",
            message=(
                f"REMAP needs the parent column {parent_table}.{parent_column} to be a "
                "masked scalar node, but it is absent from the work list."
            ),
        )

    return _remap


def _write_back_fk_column(
    child_table: pa.Table, child_column: str, values: list[object]
) -> pa.Array:
    """Materialize the resolved child FK column through the SAME pandas->Arrow
    write-back boundary `_pandas_adapter.py::_resolve_fk_node` uses: ingest the
    child's own column FK-safely (so the dtype `fk_all_null_array` may fall back
    to is the oracle's exact per-Arrow-type nullable dtype), classify the
    resolved values, assign back via the identical lossless helper, and
    round-trip the one-column frame through `pa.Table.from_pandas`. A one-column
    frame infers the same Arrow type the oracle's full frame would give this
    column, since pandas->Arrow schema inference is column-local.
    """
    one_column = pa.table({child_column: child_table.column(child_column)})
    frame = to_pandas_fk_safe(one_column, {child_column})
    safe_ints = lossless_fk_int_values(values)
    if safe_ints is not None and all(v is None for v in safe_ints):
        frame[child_column] = fk_all_null_array(len(safe_ints), frame[child_column].dtype)
    elif safe_ints is not None:
        frame[child_column] = fk_nullable_int_array(safe_ints)
    else:
        frame[child_column] = values
    resolved = pa.Table.from_pandas(frame, preserve_index=False)
    return resolved.column(child_column).combine_chunks()
