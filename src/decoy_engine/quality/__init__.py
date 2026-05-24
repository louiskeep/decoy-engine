"""V2 Distribution Integrity, Sprint D1a: Measurement Foundation.

Pure-compute distribution snapshot module. Exposes the deterministic,
JSON-serializable per-column + per-joint snapshot that later D1 sub-sprints
(diagnostic, fidelity, report assembly) consume to compare source vs output
dataframes.

This package will grow over D1b-D1d to include `diagnostic`, `fidelity`,
and `report`. D1a only lands the measurement primitive so it can be
exercised, golden-tested, and reviewed in isolation. Per Dennis-style
sub-sprint discipline: ship the smallest defensible unit, prove it, then
stack on top.

Public surface (V2.0+):
    compute_distribution_snapshot(df, *, joint_columns=None, ...) -> dict

The returned dict is keyed `schema_version = "distribution-snapshot/v1"`
so downstream consumers can branch on schema evolution without sniffing
shape. The shape is documented in `snapshot.compute_distribution_snapshot`
and pinned by tests/snapshots/test_distribution_snapshot_baseline.py.
"""

from decoy_engine.quality.snapshot import (
    DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION,
    compute_distribution_snapshot,
)

__all__ = [
    "DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION",
    "compute_distribution_snapshot",
]
