"""Progress for a chunked mask entry: one ``mask`` phase for the table plus its row counts."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from decoy_engine.progress import get_active_channel


def observe_chunked(table: str, chunks: Iterable[Any]) -> Iterator[Any]:
    """Yield ``chunks`` unchanged while counting rows (the phase is not reported yet)."""
    return get_active_channel().count_rows(table, chunks)
