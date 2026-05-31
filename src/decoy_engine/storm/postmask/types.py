"""Dataclasses for the Storm post-mask report (Reframe-A).

Lightweight + frozen so the platform's report-persistence layer can
treat them as immutable carriers. Each finding carries enough context
for the FE to render a per-finding row in the Storm tab + enough for
a future "fix this" affordance to route back to a node config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# Bump when the report dict shape changes in a non-backward-compatible
# way. The platform persists this on the JobStormReport row so reads of
# old rows can render correctly. Mirrors the Quality report's versioning.
SCHEMA_VERSION = "storm-post-mask/v1"


Severity = Literal["info", "warning", "fail", "error"]


@dataclass(frozen=True)
class ResidualPIIFinding:
    """A column that still matches a detector pattern AFTER masking.

    Severity rules:
      - ``info``: detector matched + the column WAS configured to be
        masked to a strategy that legitimately produces detectable
        values (faker name on a name column produces person_name hits;
        this is expected, not a leak).
      - ``warning``: detector matched + the column was NOT configured
        to be masked at all. Possible the operator forgot to mask a
        sensitive column.
      - ``fail``: detector matched + the column WAS configured to be
        masked to a strategy that should DESTROY the pattern (hash,
        redact, bucketize on a sensitive column), but the pattern
        survived. Indicates the mask didn't fire or failed silently.
    """

    table: str
    column: str
    detector_id: str
    match_rate: float
    severity: Severity
    configured_strategy: str | None = None  # what the plan said to do (None = no policy)
    sample_match_count: int = 0  # how many rows still match
    message: str = ""


@dataclass(frozen=True)
class FKPreservationFinding:
    """A foreign key relationship whose post-mask values don't fully resolve.

    Severity rules:
      - ``info``: 0 orphans (the check ran cleanly).
      - ``warning``: orphan rate <= 1% (some FK churn; might be intentional
        if the relationship was tagged orphan_policy: skip).
      - ``fail``: orphan rate > 1% (significant FK breakage; downstream
        joins will lose rows).
      - ``error``: relationship graph could not be walked (engine error;
        check the relationships config + the namespace registry).
    """

    parent_table: str
    parent_column: str
    child_table: str
    child_column: str
    severity: Severity
    orphan_count: int
    total_child_rows: int
    orphan_rate: float
    namespace: str | None = None
    message: str = ""


@dataclass(frozen=True)
class PolicyValidationFinding:
    """A configured mask that did not actually transform its column.

    Severity rules:
      - ``info``: column changed as expected.
      - ``fail``: column was configured to be masked but the output
        column is byte-identical to the source column. Indicates the
        mask did not fire (no-op strategy, source==output by chance
        for FPE on uniform input, or a configuration bug).
      - ``error``: could not determine source vs output (one of the
        snapshots was unavailable).

    No-op masks ARE legitimate for some configurations (passthrough on
    a column that's intentionally not masked, hash with a deterministic
    seed where the source happens to be the hash). The check raises
    ``warning`` for those + lets the operator confirm.
    """

    table: str
    column: str
    strategy: str
    severity: Severity
    source_distinct: int = 0
    output_distinct: int = 0
    bytes_identical: bool = False
    message: str = ""


@dataclass(frozen=True)
class StormPostMaskReport:
    """The full Storm post-mask report.

    Engine returns this from ``run_storm_post_mask``; the platform's
    post_mask_service serializes it to JSON for the JobStormReport row.
    The FE Storm tab (Reframe-B) joins this with the JobQualityReport
    (for distribution drift + schema integrity) and renders the union.
    """

    schema_version: str
    residual_pii: list[ResidualPIIFinding] = field(default_factory=list)
    fk_preservation: list[FKPreservationFinding] = field(default_factory=list)
    policy_validation: list[PolicyValidationFinding] = field(default_factory=list)
    # Top-level counters for the FE summary line (pass/warn/fail counts
    # rendered above the per-finding detail expander).
    pass_count: int = 0
    warning_count: int = 0
    fail_count: int = 0
    error_count: int = 0
    # If the entire pass failed catastrophically (e.g. detectors module
    # missing), this carries the exception type name. The hook still
    # produces a report row so the FE can show "Storm pass failed:
    # ModuleNotFoundError" rather than silently dropping.
    pass_failed_with: str | None = None
