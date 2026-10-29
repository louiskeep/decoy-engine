"""Cross-cutting reasons the engine assigns itself: fallbacks, outcomes and control."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Kind, Outcome, RetryClass, Severity

__all__ = [
    "CLEANUP_FAILED",
    "FINDINGS_REPORTED",
    "PUBLICATION_FAILED",
    "PUBLICATION_PARTIAL",
    "REASON_CONTEXT_TOO_LARGE",
    "RUN_CANCELLED",
    "STORAGE_CAPACITY_EXHAUSTED",
    "UNEXPECTED_ERROR",
    "UNKNOWN_REASON_FROM_FUTURE",
    "UNREGISTERED_REASON",
    "UNSUPPORTED_SCHEMA_VERSION",
    "VERIFICATION_FAILED",
    "VERIFICATION_UNAVAILABLE",
]

_d = _catalog.decoy("DecoyEngine")

UNEXPECTED_ERROR = _d("unexpected_error", "An unexpected error occurred.")
UNREGISTERED_REASON = _d(
    "unregistered_reason",
    "A reason code that this registry does not recognize.",
    retry=RetryClass.UNKNOWN,
)
UNKNOWN_REASON_FROM_FUTURE = _d(
    "unknown_reason_from_future",
    "A reason code from a newer registry version.",
    retry=RetryClass.UNKNOWN,
)
STORAGE_CAPACITY_EXHAUSTED = _d(
    "storage_capacity_exhausted",
    "The output storage ran out of space or quota.",
    outcome=Outcome.PUBLICATION,
    retry=RetryClass.ENVIRONMENT_REPAIR,
)
VERIFICATION_FAILED = _d(
    "verification_failed",
    "An enforced check rejected the output.",
    outcome=Outcome.VERIFICATION,
    retry=RetryClass.NEVER,
)
VERIFICATION_UNAVAILABLE = _d(
    "verification_unavailable",
    "The verifier could not complete.",
    outcome=Outcome.VERIFICATION,
    retry=RetryClass.UNKNOWN,
)
PUBLICATION_FAILED = _d(
    "publication_failed",
    "Staging, writing or committing the output failed.",
    outcome=Outcome.PUBLICATION,
    retry=RetryClass.UNKNOWN,
)
PUBLICATION_PARTIAL = _d(
    "publication_partial",
    "The output was partly published before a later step failed.",
    outcome=Outcome.PUBLICATION,
    retry=RetryClass.NEVER,
)
CLEANUP_FAILED = _d(
    "cleanup_failed",
    "Cleaning up after a failure did not complete.",
    outcome=Outcome.PUBLICATION,
    retry=RetryClass.ENVIRONMENT_REPAIR,
)
RUN_CANCELLED = _d(
    "run_cancelled",
    "The run was cancelled or paused by its owner.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.CANCELLED,
    retry=RetryClass.NEVER,
)
FINDINGS_REPORTED = _d(
    "findings_reported",
    "A check completed and reported findings.",
    kind=Kind.FINDING,
    severity=Severity.WARNING,
    outcome=Outcome.FINDINGS,
    retry=RetryClass.NEVER,
)
REASON_CONTEXT_TOO_LARGE = _d(
    "reason_context_too_large",
    "The reason context exceeded its size bound and was truncated.",
    severity=Severity.WARNING,
    retry=RetryClass.NEVER,
)
UNSUPPORTED_SCHEMA_VERSION = _d(
    "unsupported_schema_version",
    "A record uses a schema version this reader does not support.",
    retry=RetryClass.NEVER,
)
