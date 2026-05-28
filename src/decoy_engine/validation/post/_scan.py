"""Per-scan contract for the post-execution suite (engine-v2 S10).

Each scan in `_checks/` is a callable `(ScanContext) -> ScanOutcome`. The runner
walks the registered scans, merges every `ScanOutcome` into one `QualitySummary`
at a single site, and populates `failed_checks` from the outcomes (Dennis S10
slice-1-2 review, ruling d). `ScanContext` + `ScanOutcome` are INTERNAL to
`validation.post` (not manifest types; not in the top-level `__all__`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.validation.post._types import (
    CompositeCoherenceReport,
    DistinctCount,
    FkValidityReport,
    NullCount,
)

if TYPE_CHECKING:
    import pyarrow as pa

    from decoy_engine.plan._types import Plan
    from decoy_engine.profile import Profile
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph


@dataclass(frozen=True)
class ScanContext:
    """Everything a post-execution scan reads. Built once by the runner.

    `outputs` is the masked S9 data; `sources` is the pre-mask input. The scan
    never re-runs masking -- it scans `outputs` and compares against `sources` /
    `profile`. `profile` carries `declared_pk` + the authoritative source distinct
    / null counts; `registry` carries `CapabilityMatrix` (`format_regex`,
    `backend_type`).
    """

    plan: Plan
    outputs: dict[str, pa.Table]
    sources: Mapping[str, pa.Table]
    profile: Profile
    registry: ProviderRegistry
    relationship_graph: RelationshipGraph
    namespace_registry: NamespaceRegistry
    sample_size: int = 100  # rows per column for the sampled_values evidence (R18)


@dataclass(frozen=True)
class ScanOutcome:
    """One scan's result. `failed` is the hard-fail flag (True fails the job). The
    fragment dicts carry only the `QualitySummary` fields this scan fills; the
    runner merges them. `warnings` are scan-emitted events (e.g. a WARN-policy FK
    or a cardinality deviation) appended to the forwarded execution warnings."""

    name: str
    failed: bool
    distinct_counts: dict[str, DistinctCount] = field(default_factory=dict)
    null_counts: dict[str, NullCount] = field(default_factory=dict)
    fk_validity: dict[str, FkValidityReport] = field(default_factory=dict)
    duplicate_counts: dict[str, int] = field(default_factory=dict)
    sampled_values: dict[str, list[Any]] = field(default_factory=dict)
    composite_coherence: dict[str, CompositeCoherenceReport] = field(default_factory=dict)
    warnings: tuple[QualityWarning, ...] = ()


def column_values(table: pa.Table, column: str) -> list[Any]:
    """The column's values as a Python list, or [] if the column is absent."""
    if column not in table.column_names:
        return []
    values: list[Any] = table.column(column).to_pylist()
    return values


# Sentinel strategy label for composite-FK group columns (per_group has a
# GroupSeed, not a per-column strategy; the columns are masked, non-passthrough).
FK_GROUP_STRATEGY = "<fk_group>"


def masked_columns(plan: Plan) -> list[tuple[str, str, str]]:
    """Every masked (table, column, strategy) the plan declares.

    Per-column entries yield their `ColumnSeed.strategy`; composite-FK group
    columns (`per_group`) yield `FK_GROUP_STRATEGY`. Source-comparison scans use
    `strategy == "passthrough"` to exclude unmasked columns (their output equals
    the source by design).
    """
    out: list[tuple[str, str, str]] = []
    for table_name, table_seed in plan.seed_envelope.per_table:
        for col_name, seed in table_seed.per_column:
            out.append((table_name, col_name, seed.strategy))
        for _key, group_seed in table_seed.per_group:
            for col_name in group_seed.coherent_columns:
                out.append((table_name, col_name, FK_GROUP_STRATEGY))
    return out
