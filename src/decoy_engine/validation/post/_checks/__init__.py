"""Post-execution scan registry (engine-v2 S10).

`SCANS` is the ordered list of `(name, callable)` the runner walks. Each scan is
`(ScanContext) -> ScanOutcome`, lives in its own `_<name>.py`, and is independent
(it reads the masked output + sources/plan/profile/registry, returns its fragment
of the QualitySummary + a hard-fail flag). The runner skips any scan named in
`post_validation_skip` and merges the rest at one site.

Structural scans (slice 3a/3b) read the masked output only; source-comparison
scans (slice 4) compare against sources. The order here is the run + report order.
"""

from __future__ import annotations

from collections.abc import Callable

from decoy_engine.validation.post._checks._cardinality import run_cardinality
from decoy_engine.validation.post._checks._pk_uniqueness import run_pk_uniqueness
from decoy_engine.validation.post._scan import ScanContext, ScanOutcome

ScanFn = Callable[[ScanContext], ScanOutcome]

SCANS: tuple[tuple[str, ScanFn], ...] = (
    ("pk_uniqueness", run_pk_uniqueness),
    ("cardinality", run_cardinality),
)

__all__ = ["SCANS", "ScanFn"]
