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

    The active channel is read at call time, as a caller binds it around the call. A producer
    error is not turned into a phase event here: the phase stays open and the run's own scope
    closes it as an error, so a failure is never reported twice. Closing early is not a failure.
    """
    channel = get_active_channel()
    if not channel.active:
        return channel.count_rows(table, chunks)
    return _observe(channel, table, chunks)


def _observe(channel: ProgressChannel, table: str, chunks: Iterable[Any]) -> Iterator[Any]:
    started = channel.phase_start("mask", table=table)
    # `count_rows` is a generator function, so it can be closed.
    counted = cast(Generator[Any, None, None], channel.count_rows(table, chunks))
    rows, failed = 0, False
    try:
        for chunk in counted:
            rows += getattr(chunk, "num_rows", 0)
            yield chunk
    except BaseException as exc:
        failed = not isinstance(exc, GeneratorExit)
        raise
    finally:
        # Closing the counter first lets it flush its final rate sample before the phase ends.
        counted.close()
        if not failed:
            channel.phase_finish("mask", started, table=table, rows_out=rows)
