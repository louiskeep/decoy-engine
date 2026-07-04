"""The per-row error channel (Sprint 2 honesty pack, S5 / D7).

Established methodology (CLAUDE.md core rule): the side-channel bad-row
pattern of Spark's `badRecordsPath` / PERMISSIVE-mode corrupt-record column
and pandas `read_csv(on_bad_lines=...)`: a malformed row is routed to a named
side channel, never silently passed through, and the default remains
fail-loud. This module is that side channel for the engine's strategy
handlers.

`RowError` is what a strategy handler appends to `StrategyContext.row_errors`
(the frozen dataclass's one mutable-sink field; append, never reassign --
trap T6). `RowErrorRecord` is what the execution adapter produces after
attributing the handler's error to its table: the adapter is the only layer
that knows which table a node belongs to, so table-attribution happens at
the drain point, not inside the handler.

`reason` NEVER embeds the cell value (trap T3): "value at row 4130 is not
numeric", never "value 'SSN 543-...' is not numeric". The quarantine JSONL
is the one designed home for raw row content; error reasons travel into
manifests and logs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RowError:
    """One per-row failure recorded by a strategy handler.

    Args:
        column: Column name the handler was masking.
        row_index: 0-based position in the table's frame.
        trigger: ``"format_error"`` (a cell could not be coerced/parsed under
            the declared strategy) or ``"mask_error"`` (a per-value masking
            operation raised).
        reason: Human-readable explanation. Never embeds the cell value.
    """

    column: str
    row_index: int
    trigger: str
    reason: str


@dataclass(frozen=True)
class RowErrorRecord:
    """A table-attributed `RowError`, as carried on `ExecutionResult.row_errors`.

    Produced by the execution adapter's drain point (`drain_row_errors`),
    which is the only layer that knows a node's table.
    """

    table: str
    column: str
    row_index: int
    trigger: str
    reason: str


def drain_row_errors(sink: list[RowError], *, table: str) -> tuple[RowErrorRecord, ...]:
    """Attribute every `RowError` in `sink` to `table`, clear `sink`, and
    return the resulting `RowErrorRecord`s.

    Called by the execution adapters after EACH node dispatch (scalar,
    composite, and FK-resolve paths all route through the same drain point),
    so no row error is ever silently dropped regardless of which node
    produced it. `sink` is `ctx.row_errors`, the shared mutable list on the
    frozen `StrategyContext`; this function mutates it in place (`.clear()`)
    rather than reassigning it (trap T6: the sink's identity is what the
    handlers hold a reference to).

    Args:
        sink: The list to drain (typically `ctx.row_errors`).
        table: Table name to attribute every drained error to.

    Returns:
        Tuple of RowErrorRecord, one per drained RowError, in order.
    """
    if not sink:
        return ()
    records = tuple(
        RowErrorRecord(
            table=table, column=e.column, row_index=e.row_index, trigger=e.trigger, reason=e.reason
        )
        for e in sink
    )
    sink.clear()
    return records
