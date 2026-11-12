"""Progress for a chunked mask entry: one ``mask`` phase for the table plus its row counts.

The chunked entries hand the caller a lazy iterator, so the phase cannot be a ``with`` block
around the work. It starts when the stream is first pulled and finishes when the stream ends,
whether the caller drained it or stopped after a good chunk.
"""

from __future__ import annotations

from collections.abc import Generator, Iterable, Iterator
from typing import Any, cast

from decoy_engine.progress import ProgressChannel, get_active_channel


def observe_chunked(table: str, chunks: Iterable[Any]) -> Iterator[Any]:
    """Yield ``chunks`` unchanged while reporting ``table``'s mask phase and row counts.

    The active channel is read at call time, as a caller binds it around the call. Only a
    stream that is drained emits ``phase_finish``. A producer error, or a consumer that stops
    before the end (a write failure closes the stream half read), leaves the phase open: the
    run's own scope closes it as an error when the run fails, and the record's rule reads an
    open start on a run that succeeded as a table that ran. The late close of an abandoned
    stream therefore never reports a success.
    """
    channel = get_active_channel()
    if not channel.active:
        return channel.count_rows(table, chunks)
    return _observe(channel, table, chunks)


def _observe(channel: ProgressChannel, table: str, chunks: Iterable[Any]) -> Iterator[Any]:
    started = channel.phase_start("mask", table=table)
    # `count_rows` is a generator function, so it can be closed.
    counted = cast(Generator[Any, None, None], channel.count_rows(table, chunks))
    rows = 0
    try:
        for chunk in counted:
            rows += getattr(chunk, "num_rows", 0)
            yield chunk
    finally:
        # Closing the counter first lets it flush its final rate sample.
        counted.close()
    channel.phase_finish("mask", started, table=table, rows_out=rows)
