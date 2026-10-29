"""decoy_engine.verification - post-mask verification pass.

Public entry point:
    run_post_mask(...) -> PostMaskReport

Runs AFTER a successful mask job (residual-PII re-scan, FK preservation, policy
validation). Formerly the `storm.postmask` package; renamed by the STORM
taxonomy rename (the entrypoint is now `run_post_mask`, returning a
`PostMaskReport`). The `"storm-post-mask/v1"` schema string VALUE and the
`run_storm` config field that gates it are user-facing / persisted and are kept
unchanged. Depends on `decoy_engine.detection` only.
"""

from decoy_engine.verification.postmask import (
    SCHEMA_VERSION,
    FKPreservationFinding,
    PolicyValidationFinding,
    PostMaskReport,
    ResidualPIIFinding,
    Severity,
    run_post_mask,
)

__all__ = [
    "SCHEMA_VERSION",
    "FKPreservationFinding",
    "PolicyValidationFinding",
    "PostMaskReport",
    "ResidualPIIFinding",
    "Severity",
    "run_post_mask",
]
