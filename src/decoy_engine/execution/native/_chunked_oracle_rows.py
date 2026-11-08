"""The oracle leg's D1c sentinel counts per column, read from the handlers' own metrics.

Split out of `_chunked_entry.py` (module-size budget).
"""

from __future__ import annotations

from typing import Any


def _oracle_preserved_rows(quality_metrics: dict[str, Any]) -> dict[str, int]:
    """The oracle leg's D1c sentinel counts per column, from the handlers' own metrics."""
    return {e["column"]: e["preserved_rows"] for e in quality_metrics.get("date_preserve", ())}


def _oracle_row_counts(quality_metrics: dict[str, Any]) -> dict[str, dict[str, int]]:
    """The per-column row counts a chunk's evidence carries: D1c sentinels and entity fallbacks."""
    return {
        "preserved_rows": _oracle_preserved_rows(quality_metrics),
        "entity_fallback_rows": {
            e["column"]: e["fallback_rows"]
            for e in quality_metrics.get("date_shift_entity_fallback", ())
        },
    }
