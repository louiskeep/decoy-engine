"""Profile module: source-data shape descriptions consumed by the planner.

Public API for the data-shape side of the engine-v2 pre-execute layer. The
planner takes a Profile (this module) plus a config and emits a Plan. S1
ships the dataclasses + hash + serialization; scan logic that actually walks
the source data lands in a follow-on slice.

Source patterns:
- Profile shape draws from Apache Arrow column statistics (row_count,
  null_count, distinct_count, sampled flag) and Great Expectations result
  objects (immutable, JSON-serializable, equality-based).
- profile_hash uses plain SHA-256 over a canonical JSON serialization of
  the data-shape fields only; sidecar metadata (timestamps, version stamps,
  sampling seeds) is intentionally excluded so two profiles over identical
  source data produce equal hashes regardless of when they were taken.

See docs/v2/sprints/engine-v2/sprint-01-profile-plan-and-fixtures.md in
decoy-platform for the full S1 spec and the design rationale behind the
hash-scope decision (resolution of B1 in the Dennis review).
"""

from __future__ import annotations

from decoy_engine.profile._csv_reader import (
    CsvDialect,
    read_csv_bytes,
    read_csv_source,
    resolve_csv_dialect,
)
from decoy_engine.profile._declared_columns import (
    DeclarationFinding,
    DeclarationRequest,
    PreparedDeclaredSources,
    check_columns_declared,
    declaration_findings,
    declared_columns,
    prepare_declared_sources,
    source_columns,
)
from decoy_engine.profile._hash import profile_hash
from decoy_engine.profile._readers import (
    LazySource,
    ProfileSource,
    RowCount,
    build_declared_source,
    build_profile_source,
)
from decoy_engine.profile._serialize import profile_from_json, profile_to_json
from decoy_engine.profile._source import profile_relationships_from_config, profile_source
from decoy_engine.profile._types import (
    ColumnProfile,
    PIIClass,
    Profile,
    Relationship,
    TableProfile,
)

__all__ = [
    "ColumnProfile",
    "CsvDialect",
    "DeclarationFinding",
    "DeclarationRequest",
    "LazySource",
    "PIIClass",
    "PreparedDeclaredSources",
    "Profile",
    "ProfileSource",
    "Relationship",
    "RowCount",
    "TableProfile",
    "build_declared_source",
    "build_profile_source",
    "check_columns_declared",
    "declaration_findings",
    "declared_columns",
    "prepare_declared_sources",
    "profile_from_json",
    "profile_hash",
    "profile_relationships_from_config",
    "profile_source",
    "profile_to_json",
    "read_csv_bytes",
    "read_csv_source",
    "resolve_csv_dialect",
    "source_columns",
]
