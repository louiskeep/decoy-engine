"""Caller-managed residency signalling for the out-of-core route (P4-A, Option A).

The OOC route's memory bound -- engine-controlled peak residency bounded with
respect to table row cardinality -- holds only for the structural bounded shape:
every source a `LazySource` (never fully materialized) AND a sink that consumes
`write_batches` incrementally without retaining the stream (`ParquetTransactional
Sink` is the production-proven such sink). A resident `pa.Table` source, a
missing sink, or a `source_loader` is caller-managed: the route runs it unchanged,
but its peak memory is the caller's responsibility. Four cross-model plan-gate
rounds established that a precise fail-closed byte guard cannot make the bound
absolute for arbitrary in-process callers, so the route signals these shapes
rather than policing them.

The signal is a structured `QualityWarning` in `ExecutionResult.warnings` (the
route's own warning channel, folded into the manifest downstream). It is
control-flow-neutral by construction: it rides in the returned result and cannot
alter execution or be escalated by a caller's `-W error` filter, unlike a stdlib
`warnings.warn`. Adding a new `code` to the S5-owned `QualityWarning` is within
its contract (its docstring: "Codes added by later sprints, same shape, same
emission channel").
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa

from decoy_engine.generation.pool._events import QualityWarning

__all__ = [
    "RESIDENCY_WARNING_CODE",
    "caller_managed_residency_shapes",
    "residency_quality_warning",
    "residency_warning_message",
]

#: The `QualityWarning.code` a caller filters on to find the residency heads-up.
RESIDENCY_WARNING_CODE = "out_of_core_caller_managed_residency"


def caller_managed_residency_shapes(
    sources: Mapping[str, Any], *, sink: Any, source_loader: Any
) -> tuple[str, ...]:
    """The caller-managed input shapes present, or () for the bounded shape.

    Residency is detected by TYPE (`pa.Table` is resident; a `LazySource`
    streams), not by whether `sources` is non-empty -- the guaranteed
    LazySource+sink shape passes a non-empty `sources` of LazySources and must NOT
    read as resident.
    """
    shapes: list[str] = []
    if any(isinstance(value, pa.Table) for value in sources.values()):
        shapes.append("a resident pa.Table source")
    if sink is None:
        shapes.append("no sink (the whole output is held resident)")
    if source_loader is not None:
        shapes.append("a source_loader (returns an unbounded resident table)")
    return tuple(shapes)


def residency_warning_message(shapes: tuple[str, ...]) -> str:
    """The heads-up naming the shape(s) and the bounded alternative.

    Empty in, empty out: with no caller-managed shape there is nothing to warn,
    so a direct caller cannot build a malformed "...for this call: ." message.
    """
    if not shapes:
        return ""
    return (
        "out-of-core residency bound not guaranteed for this call: "
        + "; ".join(shapes)
        + ". Engine-controlled peak residency is bounded with respect to table "
        "row cardinality only when every source is a LazySource and the sink "
        "consumes write_batches incrementally without retaining the stream "
        "(e.g. ParquetTransactionalSink). To run bounded, pass a LazySource per "
        "table plus such a sink; or force execution_mode='full_frame' to run "
        "resident at your own memory risk."
    )


def residency_quality_warning(shapes: tuple[str, ...]) -> QualityWarning:
    """The structured, control-flow-neutral residency warning for `.warnings`.

    Route-level, not provider-attributed (`provider=""`); the shapes and the
    human-readable message ride in `detail`.
    """
    return QualityWarning(
        code=RESIDENCY_WARNING_CODE,
        provider="",  # route-level, not provider-attributed; column defaults to None
        detail={"shapes": list(shapes), "message": residency_warning_message(shapes)},
    )
