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

# Scans whose privacy check is value-MEMBERSHIP against the source (a source
# value reappearing in the masked output is a leak), NOT a positional / row-count
# comparison. The runner hands these the FULL pre-quarantine source: quarantine
# can drop a source row whose value still appears in a RETAINED masked row, and an
# aligned (post-quarantine) source would no longer contain that value, hiding a
# real substitution leak. Positional / row-count scans (null_audit,
# determinism_sample) instead need the row-ALIGNED source so a successful
# quarantine is not read as a false failure -- they are NOT listed here. A new
# value-membership scan MUST be added to this set or a quarantine could hide its
# leaks (the source-selection seam is wiring, not scan-internal logic).
FULL_SOURCE_SCANS: frozenset[str] = frozenset({"leakage"})

__all__ = ["FULL_SOURCE_SCANS", "SCANS", "ScanFn"]
