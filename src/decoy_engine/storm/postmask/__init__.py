"""Storm post-mask check pass (Reframe-A; PO lock 2026-05-30).

This subpackage runs AFTER a successful mask job to produce a
JobStormReport. It is the engine half of the "per-pipeline opt-in
post-mask check" reframe. The platform runner calls
``run_storm_post_mask`` when ``pipeline.run_storm`` is true; the
returned dict is persisted as the JobStormReport row.

Three net-new check categories ship here:

- ``residual_pii``: re-runs the Storm detectors over the masked
  output. Flags columns that still match a detector pattern AND
  weren't configured to produce that pattern (faker name on a
  person_name column is expected; an unconfigured detector hit
  on a column the operator forgot to mask is a finding). Source-
  aware: detector-flagged columns are compared positionally against
  the source frames, and output==source identity escalates to
  'fail' -- a silently-failed mask cannot hide behind a PII-like-
  producer strategy.
- ``fk_preservation``: walks the relationships graph against the
  masked output; counts orphan FKs, classifies severity.
- ``policy_validation``: reads the compiled plan; verifies every
  configured mask actually changed the output column vs. the source
  column (catches no-op masks).

Two more categories (distribution drift + schema integrity) are
NOT computed here -- per Reframe-A spec Finding R-4, the FE reads
those from the existing ``JobQualityReport``. Storm reports + Quality
reports are joined at the UI layer.

Best-effort contract: ``run_storm_post_mask`` raises only on
programming errors (TypeError, missing required arg). Detector /
relationships failures are captured as findings with severity
``error``; they do NOT fail the mask job. This matches the Quality
hook precedent.

Schema version: ``storm-post-mask/v1``. Bump when the report dict
shape changes in a non-backward-compatible way.
"""

from __future__ import annotations

from decoy_engine.storm.postmask.runner import run_storm_post_mask
from decoy_engine.storm.postmask.types import (
    SCHEMA_VERSION,
    FKPreservationFinding,
    PolicyValidationFinding,
    ResidualPIIFinding,
    Severity,
    StormPostMaskReport,
)

__all__ = [
    "SCHEMA_VERSION",
    "FKPreservationFinding",
    "PolicyValidationFinding",
    "ResidualPIIFinding",
    "Severity",
    "StormPostMaskReport",
    "run_storm_post_mask",
]
