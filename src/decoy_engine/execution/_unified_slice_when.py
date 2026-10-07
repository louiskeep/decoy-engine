"""`when:` support for the unified full-frame route: admission and the row masks.

Admission reuses the chunked route's per-column verdict (`when_native_rejection`), so both
native routes accept the same columns. The masks come from the oracle's own predicate
function run once per table on the oracle's own frame, which makes them equal to the
oracle's masks by construction for any reference type the verdict admits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa
from pandas.core.computation.parsing import clean_column_name

from decoy_engine.execution._column_access import has_when
from decoy_engine.execution._when_gate import _eval_predicate
from decoy_engine.execution.native._when_admission import (
    parsed_when,
    when_native_rejection,
)
from decoy_engine.expressions._when_parser import when_column_refs

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    import pandas as pd

__all__ = ["WhenMasks", "compute_when_masks", "when_columns_admitted"]


@dataclass(frozen=True)
class WhenMasks:
    """Every `when:` node's row mask, keyed by `node_id`, in the two forms its consumers need.

    Both forms hold the same values, with predicate nulls unselected. `selected` (NumPy
    `bool`) indexes the pandas frame for the oracle's write-back; `arrow` slices per batch
    for the kernel step and filters the kernel output.
    """

    selected: dict[str, np.ndarray[Any, np.dtype[np.bool_]]] = field(default_factory=dict)
    arrow: dict[str, pa.Array] = field(default_factory=dict)


def when_columns_admitted(
    entries: Sequence[Mapping[str, Any]], source: pa.Table, registry: Any, *, table: str
) -> bool:
    """Whether every `when:` column of the table can run on the unified route.

    A table without a `when:` column is admitted here unconditionally. Otherwise each
    `when:` column must pass the chunked route's verdict, read only columns the source has,
    and the source must hold no two names pandas' eval resolver maps to one key, since eval
    would then let one column shadow another and the verdict only tracks physical names.
    """
    when_entries = [e for e in entries if has_when(e)]
    if not when_entries:
        return True
    names = [str(e.get("name")) for e in entries]
    if len({clean_column_name(n) for n in names}) != len(names):
        return False
    for entry in when_entries:
        column = str(entry["name"])
        if when_native_rejection(column, entries, registry, table=table, schema=source.schema):
            return False
        ast = parsed_when(entry)
        if ast is None or not set(when_column_refs(ast)) <= set(source.column_names):
            return False
    return True


def compute_when_masks(frame: pd.DataFrame, nodes: Iterable[Any]) -> WhenMasks:
    """The mask of every `PhysicalNode` whose binding carries a predicate. `nodes` is untyped
    here because this module stays clear of the physical seam's import sentry.

    The predicate runs through the oracle's `_eval_predicate` on the FULL frame, so pandas
    resolves names exactly as the oracle does. Any failure propagates to the caller, which
    declines the table before a node runs so the oracle reports the first error itself.
    """
    from decoy_engine.execution._unified_slice import UnifiedSliceInvariantError

    masks = WhenMasks()
    for node in nodes:
        binding = node.execution
        if binding is None or binding.when_expression is None:
            continue
        series = _eval_predicate(
            frame, binding.when_expression, node.strategy, column=node.columns[0]
        )
        selected = series.to_numpy(dtype=bool, na_value=False)
        if len(selected) != len(frame):
            raise UnifiedSliceInvariantError(
                f"unified slice: a when mask has {len(selected)} rows for a {len(frame)}-row frame"
            )
        masks.selected[node.node_id] = selected
        masks.arrow[node.node_id] = pa.array(selected, type=pa.bool_())
    return masks
