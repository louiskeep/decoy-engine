"""Bounded, value-free summary of per-row masking errors (OBS-2c).

A `RowErrorRecord` carries a free-text `reason`. This summary never does: it holds
schema identifiers (table, column), the closed trigger codes, counts and positions, so
it is safe to persist and narrate. Positions on the chunked legs are chunk-local, so
each sample item records the chunk index and the chunk's absolute row offset.

`total_observed` / `covered` / `uncovered` are the run's observed totals and are
distinct from the records handed in (the uncovered subset on a failed run, one chunk's
records on a chunked leg); `omitted` counts only the records handed in that the sample
did not hold.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

DEFAULT_SAMPLE_LIMIT = 50
MAX_SAMPLE_LIMIT = 1000

# The closed trigger vocabulary (`RowError.trigger`); anything else is reported as `other`.
KNOWN_TRIGGERS: frozenset[str] = frozenset({"format_error", "mask_error"})
OTHER_TRIGGER = "other"


class RowErrorCompleteness(str, Enum):
    """What the summary observed, independent of whether the job succeeded."""

    COMPLETE = "complete"
    PARTIAL_BEFORE_ABORT = "partial_before_abort"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RowErrorSampleItem:
    column: str
    trigger: str
    row_index: int
    chunk_index: int | None
    chunk_row_offset: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "trigger": self.trigger,
            "row_index": self.row_index,
            "chunk_index": self.chunk_index,
            "chunk_row_offset": self.chunk_row_offset,
        }


@dataclass(frozen=True)
class RowErrorSummary:
    table: str
    by_column: Mapping[str, Mapping[str, int]]
    total_observed: int
    covered: int
    uncovered: int
    sample: tuple[RowErrorSampleItem, ...]
    sample_limit: int
    omitted: int
    completeness: RowErrorCompleteness = field(default=RowErrorCompleteness.COMPLETE)

    def __post_init__(self) -> None:
        if min(self.total_observed, self.covered, self.uncovered, self.omitted) < 0:
            raise ValueError("row-error summary counts must be >= 0")
        if self.total_observed != self.covered + self.uncovered:
            raise ValueError("total_observed must equal covered + uncovered")
        if len(self.sample) > self.sample_limit:
            raise ValueError("sample is longer than sample_limit")

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form; the key set is frozen (plan rev 5, R5.3)."""
        return {
            "table": self.table,
            "by_column": {c: dict(t) for c, t in self.by_column.items()},
            "total_observed": self.total_observed,
            "covered": self.covered,
            "uncovered": self.uncovered,
            "sample": [s.to_dict() for s in self.sample],
            "sample_limit": self.sample_limit,
            "omitted": self.omitted,
            "completeness": self.completeness.value,
        }


def _trigger_code(trigger: str) -> str:
    return trigger if trigger in KNOWN_TRIGGERS else OTHER_TRIGGER


def summarize_row_errors(
    records: Iterable[Any],
    *,
    table: str,
    columns: Collection[str],
    completeness: RowErrorCompleteness,
    total_observed: int,
    covered: int,
    uncovered: int,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
    chunk_index: int | None = None,
    chunk_row_offset: int | None = None,
) -> RowErrorSummary:
    """Summarize `records` (all of `table`) without their `reason` text.

    `columns` is the table's schema; a record outside `table` or `columns` raises, which
    bounds `by_column` by columns x the closed trigger vocabulary. The sample is the first
    `sample_limit` (clamped to `MAX_SAMPLE_LIMIT`) records ordered by
    (column, chunk_index, row_index)."""
    if sample_limit <= 0:
        raise ValueError("sample_limit must be a positive integer")
    limit = min(sample_limit, MAX_SAMPLE_LIMIT)
    schema = frozenset(columns)
    counts: dict[str, dict[str, int]] = {}
    items: list[RowErrorSampleItem] = []
    for rec in records:
        if rec.table != table:
            raise ValueError(f"row error attributed to table {rec.table!r}, expected {table!r}")
        if rec.column not in schema:
            raise ValueError(f"row error column {rec.column!r} is not a column of {table!r}")
        trigger = _trigger_code(rec.trigger)
        per_trigger = counts.setdefault(rec.column, {})
        per_trigger[trigger] = per_trigger.get(trigger, 0) + 1
        items.append(
            RowErrorSampleItem(rec.column, trigger, rec.row_index, chunk_index, chunk_row_offset)
        )
    items.sort(
        key=lambda s: (s.column, -1 if s.chunk_index is None else s.chunk_index, s.row_index)
    )
    sample = tuple(items[:limit])
    return RowErrorSummary(
        table=table,
        by_column={c: dict(sorted(t.items())) for c, t in sorted(counts.items())},
        total_observed=total_observed,
        covered=covered,
        uncovered=uncovered,
        sample=sample,
        sample_limit=limit,
        omitted=max(0, len(items) - len(sample)),
        completeness=completeness,
    )


def chunk_row_error_summary(
    records: Collection[Any],
    *,
    table: str,
    columns: Collection[str],
    chunk_index: int,
    chunk_row_offset: int,
) -> RowErrorSummary:
    """Summary for the first error-bearing chunk of a chunked leg, which stops there.

    Later chunks were never read, so the run is PARTIAL_BEFORE_ABORT and no record is
    quarantine-covered (the chunked path has no quarantine)."""
    return summarize_row_errors(
        records,
        table=table,
        columns=columns,
        completeness=RowErrorCompleteness.PARTIAL_BEFORE_ABORT,
        total_observed=len(records),
        covered=0,
        uncovered=len(records),
        chunk_index=chunk_index,
        chunk_row_offset=chunk_row_offset,
    )


def summarize_observed_by_table(
    observed: Iterable[Any],
    uncovered: Iterable[Any],
    *,
    columns_by_table: Mapping[str, Collection[str]],
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
) -> dict[str, RowErrorSummary]:
    """One COMPLETE summary per table from every observed record of a full-frame run.

    `uncovered` is the subset the quarantine config did not cover; the rest is covered.
    The counts come from the records themselves, never from the `quality_metrics`
    `row_errors` keys, which concatenate unescaped identifiers."""
    uncovered_ids = {id(r) for r in uncovered}
    grouped: dict[str, list[Any]] = {}
    for rec in observed:
        grouped.setdefault(rec.table, []).append(rec)
    out: dict[str, RowErrorSummary] = {}
    for table in sorted(grouped):
        recs = grouped[table]
        n_uncovered = sum(1 for r in recs if id(r) in uncovered_ids)
        known = columns_by_table.get(table)
        out[table] = summarize_row_errors(
            recs,
            table=table,
            columns=known if known is not None else {r.column for r in recs},
            completeness=RowErrorCompleteness.COMPLETE,
            total_observed=len(recs),
            covered=len(recs) - n_uncovered,
            uncovered=n_uncovered,
            sample_limit=sample_limit,
        )
    return out


__all__ = [
    "DEFAULT_SAMPLE_LIMIT",
    "MAX_SAMPLE_LIMIT",
    "RowErrorCompleteness",
    "RowErrorSampleItem",
    "RowErrorSummary",
    "chunk_row_error_summary",
    "summarize_observed_by_table",
    "summarize_row_errors",
]
