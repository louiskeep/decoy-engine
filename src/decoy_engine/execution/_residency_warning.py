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
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa

__all__ = [
    "CallerManagedResidencyWarning",
    "caller_managed_residency_shapes",
    "residency_warning_message",
]


class CallerManagedResidencyWarning(UserWarning):
    """The OOC route ran a caller-managed input shape it cannot memory-bound.

    A resident `pa.Table` source is RAM the caller already spent before the route
    saw it; a no-sink output is RAM the caller asked to receive; a `source_loader`
    returns an unbounded resident table. The route runs the job unchanged; this is
    a best-effort heads-up that NEVER alters the result or control flow. A caller's
    own `-W error` filter can escalate this stdlib warning into an exception; the
    always-present, control-flow-neutral record is `quality_metrics["residency"]`.
    """


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
    """The heads-up naming the shape(s) and the bounded alternative."""
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
