"""Resolve ``windowed_date`` anchor cells against the pinned reference date.

``pd.Timestamp`` accepts ``"now"``/``"today"`` (wall clock) and fills a time-only string such as
``"12:00"`` with the current date. Pinned mode substitutes the plan's reference instant for the
two tokens and refuses every other non-absolute form before pandas sees it.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.generation._pinned_compile import ISO_ANCHOR
from decoy_engine.generation._pinned_plan import PinnedModeError


def resolve_anchor_cells(values: list[Any], reference_date: str) -> list[Any]:
    """``reference_date`` is the plan's ``YYYY-MM-DDTHH:MM:SSZ`` text."""
    instant = reference_date.rstrip("Z")
    day = instant.split("T", 1)[0]
    out: list[Any] = []
    for position, cell in enumerate(values):
        if isinstance(cell, str):
            form = cell.strip().lower()
            if form == "now":
                cell = instant
            elif form == "today":
                cell = day
            elif ISO_ANCHOR.match(cell) is None:
                # Position only: the cell text stays out of the message.
                raise PinnedModeError(
                    code="pinned_windowed_anchor_not_absolute",
                    message=f"windowed_date anchor at row {position} is not an absolute date",
                )
        out.append(cell)
    return out
