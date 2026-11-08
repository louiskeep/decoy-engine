"""The oracle leg's D1c sentinel counts per column, read from the handlers' own metrics.

Split out of `_chunked_entry.py` (module-size budget).
"""

from __future__ import annotations

from collections.abc import Iterable
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


def entity_fallback_metric(
    table: str, evidence: dict[str, Any], chunk_results: Iterable[Any]
) -> dict[str, Any]:
    """The run-level ``date_shift_entity_fallback`` metric of a chunked run, or {} when none.

    Summed from the route evidence (dispatcher and streamed lanes); the legacy lane builds its
    evidence from config, so there the chunks' own handler metrics are summed. Either way the
    totals equal what a full-frame run of the same rows reports.
    """
    counts: dict[str, int] = {
        c["column"]: c["entity_fallback_rows"]
        for c in evidence.get("columns", ())
        if "entity_fallback_rows" in c
    }
    if not counts:
        for result in chunk_results:
            for e in result.quality_metrics.get("date_shift_entity_fallback", ()):
                counts[e["column"]] = counts.get(e["column"], 0) + e["fallback_rows"]
    if not counts:
        return {}
    return {
        "date_shift_entity_fallback": [
            {"table": table, "column": column, "fallback_rows": n}
            for column, n in sorted(counts.items())
        ]
    }
