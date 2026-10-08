"""Exact integer keys for deterministic Faker over integer columns that hold nulls.

pandas widens an Arrow integer column with a null to float64, and deterministic keying
refuses a float, so the job fails. The shared frame has other readers (`when:` predicates,
derived columns, group_by siblings), so it stays as it is. The adapter instead records the
column's original Arrow values per table or chunk, and the Faker handler keys from those.
The canonical bytes then equal those of the same value in a null-free int64 column.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.generation.pool._errors import GenerationError
from decoy_engine.plan._types import ColumnSeed

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import StrategyContext
    from decoy_engine.execution._runner import WorkNode


def _widened_int_column(source: pa.Table, frame: pd.DataFrame, column: str) -> bool:
    """True when `column` is an Arrow integer that pandas made float64 around a null.

    An all-null or empty column is left alone: it already masks, and nothing in it needs
    an exact key.
    """
    if column not in source.schema.names or column not in frame.columns:
        return False
    arrow_col = source.column(column)
    return (
        pa.types.is_integer(arrow_col.type)
        and frame[column].dtype == np.float64
        and 0 < arrow_col.null_count < len(arrow_col)
    )


def exact_int_faker_sources(
    table: str, source: pa.Table, frame: pd.DataFrame, nodes: Iterable[WorkNode]
) -> dict[tuple[str, str], pa.ChunkedArray]:
    """Original Arrow columns for the table's exact-int Faker columns.

    `nodes` are the table's work nodes in the adapter's real dispatch order. A column an
    earlier node declared as written no longer holds the converted source, so it keeps
    the behavior it has without this fix.
    """
    out: dict[tuple[str, str], pa.ChunkedArray] = {}
    written: set[str] = set()
    for node in nodes:
        column = node.columns[0]
        plan = node.plan_slice
        if (
            node.kind == "scalar"
            and node.strategy == "faker"
            and isinstance(plan, ColumnSeed)
            and plan.deterministic
            and column not in written
            and _widened_int_column(source, frame, column)
        ):
            out[(table, column)] = source.column(column)
        written.update(node.columns)
    return out


def register_exact_int_sources(
    ctx: StrategyContext,
    table: str,
    source: pa.Table,
    frame: pd.DataFrame,
    nodes: Iterable[WorkNode],
) -> None:
    """Record the table's exact-int Faker columns on the context, for its frame's lifetime."""
    ctx.exact_int_sources.update(exact_int_faker_sources(table, source, frame, nodes))


def release_table(sources: dict[tuple[str, str], pa.ChunkedArray], table: str) -> None:
    for key in [k for k in sources if k[0] == table]:
        del sources[key]


def selected_positions(mask: pd.Series[Any]) -> np.ndarray[Any, Any]:
    """Row positions a boolean mask selects, with a missing value selecting nothing.

    This is exactly what `df.loc[mask]` selects for a nullable boolean mask.
    """
    return np.flatnonzero(mask.to_numpy(dtype=bool, na_value=False))


def gated_context(
    ctx: StrategyContext, column: str, positions: np.ndarray[Any, Any]
) -> StrategyContext:
    """A one-call copy of `ctx` carrying the gate's full-table row positions.

    A positional strategy (categorical, REUSE Faker, windowed_date) keys each selected row on
    its full-table row, so every gated call carries the positions (C8-iii-d). A deterministic
    exact-integer Faker column also reads them, through `sampling_source`. The sinks stay shared.
    """
    return dataclasses.replace(ctx, gate_positions=positions)


def sampling_source(
    ctx: StrategyContext, column: str, frame_column: pd.Series[Any], *, deterministic: bool
) -> pd.Series[Any]:
    """The Series the deterministic sampler keys from: exact Arrow ints when registered.

    Python ints for valid rows and None for nulls, full length, in the frame column's index.
    The dtype is set explicitly because inference would widen to float again.
    """
    # A nested child call samples synthetic leaves under a placeholder column name; it must
    # never pick up a real column's exact integers that happen to share that name.
    nested = bool(getattr(ctx, "nested_outer_column", ""))
    key = (ctx.current_table, column)
    exact = ctx.exact_int_sources.get(key) if deterministic and not nested else None
    if exact is None:
        return frame_column
    taken = exact if ctx.gate_positions is None else exact.take(pa.array(ctx.gate_positions))
    if len(taken) != len(frame_column) or not np.array_equal(
        taken.is_null().to_numpy(), frame_column.isna().to_numpy()
    ):
        raise GenerationError(
            code="exact_int_null_mask_mismatch",
            message=(
                f"exact integer source for column {column!r} does not line up with the frame "
                "(row count or null positions differ); refusing to key from it."
            ),
        )
    return pd.Series(taken.to_pylist(), index=frame_column.index, dtype=object)
