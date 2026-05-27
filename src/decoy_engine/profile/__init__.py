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

from decoy_engine.profile._hash import profile_hash
from decoy_engine.profile._serialize import profile_from_json, profile_to_json
from decoy_engine.profile._source import profile_source
from decoy_engine.profile._types import (
    ColumnProfile,
    PIIClass,
    Profile,
    Relationship,
    TableProfile,
)

__all__ = [
    "ColumnProfile",
    "PIIClass",
    "Profile",
    "Relationship",
    "TableProfile",
    "profile_from_json",
    "profile_hash",
    "profile_source",
    "profile_to_json",
]
