"""Post-scan integrity check pass (Reframe-A; PO lock 2026-05-30).

This subpackage runs AFTER a successful mask job to produce a
JobPostScanIntegrityReport. It is the engine half of the "per-pipeline opt-in
post-mask check" reframe. The platform runner calls
``run_post_mask`` when ``pipeline.post_scan.integrity`` is true; the
returned dict is persisted as the JobPostScanIntegrityReport row.

Three net-new check categories ship here:

- ``residual_pii``: re-runs the detectors over the masked
  output. Flags columns that still match a detector pattern AND
  weren't configured to produce that pattern (faker name on a
  person_name column is expected; an unconfigured detector hit
  on a column the operator forgot to mask is a finding). Source-
  aware: detector-flagged columns are compared positionally against
  the source frames, and output==source identity escalates to
  'fail' -- a silently-failed mask cannot hide behind a PII-like-
  producer strategy.
- ``fk_preservation``: one finding per declared edge, derived from the
  declared keys' facts (orphans the run introduced fail).
- ``policy_validation``: reads the compiled plan; verifies every
  configured mask actually changed the output column vs. the source
  column (catches no-op masks).

Two more categories (distribution drift + schema integrity) are
NOT computed here -- per Reframe-A spec Finding R-4, the FE reads
those from the existing ``JobQualityReport``. Post-scan integrity reports + Quality
reports are joined at the UI layer.

Best-effort contract: ``run_post_mask`` raises only on
programming errors (TypeError, missing required arg). Detector /
relationships failures are captured as findings with severity
``error``; they do NOT fail the mask job. This matches the Quality
hook precedent.

Schema version: ``post-scan-integrity/v2``. Bump when the report dict
shape changes in a non-backward-compatible way.
"""

from __future__ import annotations

from decoy_engine.verification.postmask._blocked import (
    PostScanBlockedError,
    PostScanVerdict,
    blocked_error,
    post_scan_verdict,
    severity_counts,
)
from decoy_engine.verification.postmask._keys import (
    KeyCounts,
    KeyResult,
    check_keys,
    key_columns,
    keys_report,
)
from decoy_engine.verification.postmask._stream_input import (
    TableBatches,
    from_lazy,
    from_staged,
    from_table,
    key_batches,
    missing_columns,
    with_cast,
)
from decoy_engine.verification.postmask.runner import run_post_mask, run_post_mask_tables
from decoy_engine.verification.postmask.types import (
    SCHEMA_VERSION,
    FKPreservationFinding,
    PolicyValidationFinding,
    PostMaskReport,
    ResidualPIIFinding,
    Severity,
)

__all__ = [
    "SCHEMA_VERSION",
    "FKPreservationFinding",
    "KeyCounts",
    "KeyResult",
    "PolicyValidationFinding",
    "PostMaskReport",
    "PostScanBlockedError",
    "PostScanVerdict",
    "ResidualPIIFinding",
    "Severity",
    "TableBatches",
    "blocked_error",
    "check_keys",
    "from_lazy",
    "from_staged",
    "from_table",
    "key_batches",
    "key_columns",
    "keys_report",
    "missing_columns",
    "post_scan_verdict",
    "run_post_mask",
    "run_post_mask_tables",
    "severity_counts",
    "with_cast",
]
