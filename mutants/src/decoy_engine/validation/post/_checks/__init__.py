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
from decoy_engine.validation.post._checks._composite_coherence import run_composite_coherence
from decoy_engine.validation.post._checks._determinism_sample import run_determinism_sample
from decoy_engine.validation.post._checks._fk_validity import run_fk_validity
from decoy_engine.validation.post._checks._format_rules import run_format_rules
from decoy_engine.validation.post._checks._leakage import run_leakage
from decoy_engine.validation.post._checks._null_audit import run_null_audit
from decoy_engine.validation.post._checks._pk_uniqueness import run_pk_uniqueness
from decoy_engine.validation.post._checks._sampled_values import run_sampled_values
from decoy_engine.validation.post._scan import ScanContext, ScanOutcome

ScanFn = Callable[[ScanContext], ScanOutcome]

# The 8 scans + the sampled_values evidence step, in run + report order.
SCANS: tuple[tuple[str, ScanFn], ...] = (
    ("pk_uniqueness", run_pk_uniqueness),
    ("cardinality", run_cardinality),
    ("format_rules", run_format_rules),
    ("composite_coherence", run_composite_coherence),
    ("null_audit", run_null_audit),
    ("leakage", run_leakage),
    ("fk_validity", run_fk_validity),
    ("determinism_sample", run_determinism_sample),
    ("sampled_values", run_sampled_values),
)

__all__ = ["SCANS", "ScanFn"]
