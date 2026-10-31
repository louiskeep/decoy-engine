"""decoy_engine.verification - post-mask verification pass.

Public entry point:
    run_post_mask(...) -> PostMaskReport

Runs AFTER a successful mask job (residual-PII re-scan, FK preservation, policy
validation). Post-scan phase: the entrypoint is `run_post_mask`, returning a
`PostMaskReport` under the `"post-scan-integrity/v2"` schema, gated by the
`post_scan.integrity` config field. Depends on `decoy_engine.detection` only.
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
