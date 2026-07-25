"""quality_summary manifest-block dataclasses (engine-v2 S10).

The post-execution scan suite aggregates its findings into a `QualitySummary`,
which serializes into the manifest's `quality_summary` block (cross-sprint
contracts §2.14). `quality_warnings` reuses the SHIPPED S5 `QualityWarning` type
(R14: S5 owns the shape, S10 forwards) -- no redefinition. `distinct_counts` /
`null_counts` carry a source/output PAIR per column so the comparison is not
collapsed into a single int.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from decoy_engine.generation.pool._events import QualityWarning


@dataclass(frozen=True)
class DistinctCount:
    """Per-column distinct-value counts on the source vs the masked output."""

    source_distinct: int
    output_distinct: int


@dataclass(frozen=True)
class NullCount:
    """Per-column null counts on the source vs the masked output."""

    source_nulls: int
    output_nulls: int


@dataclass(frozen=True)
class FkValidityReport:
    """Per-relationship FK-resolution audit (cross-sprint contracts §3 row 8)."""

    relationship: str  # "parent_table.parent_columns -> child_table.child_columns"
    namespace: str
    orphan_policy: str
    child_row_count: int
    parent_match_count: int  # child FK rows that resolve to a masked parent
    orphan_count: int  # non-null child FK rows with no parent (PRESERVE/WARN/REMAP)
    invalid_count: int  # non-null child FK absent from parent (a FAIL would have raised)


@dataclass(frozen=True)
class CompositeCoherenceReport:
    """Per-composite-generator post-mask coherence audit (e.g. email == first.last;
    (city, state, zip) a verbatim locality-table row)."""

    generator: str
    columns: tuple[str, ...]
    total_rows: int
    coherent_rows: int
    incoherent_rows: int


@dataclass(frozen=True)
class QualitySummary:
    """The `quality_summary` manifest block. Built by the PostValidationRunner from
    the S9 `ExecutionResult` (`.outputs` for masked data, `.warnings` for events)
    plus the sources. Scan-populated fields are empty until their scan runs."""

    distinct_counts: dict[str, DistinctCount]
    null_counts: dict[str, NullCount]
    fk_validity: dict[str, FkValidityReport]
    duplicate_counts: dict[str, int]
    sampled_values: dict[str, list[Any]]
    quality_warnings: tuple[QualityWarning, ...]
    composite_coherence: dict[str, CompositeCoherenceReport]
    timing_per_phase: dict[str, float]
    failed_checks: tuple[str, ...] = field(default=())
