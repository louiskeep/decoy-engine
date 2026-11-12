"""Cross-cutting reasons the engine assigns itself: fallbacks, outcomes and control."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Kind, Outcome, RetryClass, Severity

__all__ = [
    "CLEANUP_FAILED",
    "CONNECTOR_PERMANENT_FAILURE",
    "CONNECTOR_TRANSIENT_FAILURE",
    "FILE_ACCESS_DENIED",
    "FINDINGS_REPORTED",
    "ISOLATED_RUN_TIMED_OUT",
    "ISOLATED_WORKER_DIED",
    "ISOLATED_WORKER_OUT_OF_MEMORY",
    "PUBLICATION_ATTEMPT_ABORTED",
    "PUBLICATION_ATTEMPT_SEALED",
    "PUBLICATION_COMMIT_IN_PROGRESS",
    "PUBLICATION_COMMIT_NOT_VISIBLE",
    "PUBLICATION_COMMIT_UNCERTAIN",
    "PUBLICATION_FAILED",
    "PUBLICATION_LOCK_TIMEOUT",
    "PUBLICATION_LOCK_UNAVAILABLE",
    "PUBLICATION_OPERATION_CONFLICT",
    "PUBLICATION_PARTIAL",
    "PUBLICATION_RECOVERY_IN_PROGRESS",
    "PUBLICATION_TARGET_OCCUPIED",
    "PUBLICATION_VAULT_CONFLICT",
    "PUBLICATION_VAULT_MISMATCH",
    "PUBLICATION_WRITE_IN_PROGRESS",
    "REASON_CONTEXT_TOO_LARGE",
    "REASON_RECORD_INVALID",
    "ROW_ERRORS_FAILED",
    "RUN_CANCELLED",
    "RUN_PAUSED",
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
# The file-access policy's one denial (`decoy_engine.fileaccess.FileAccessDenied`): the principal
# may not read or write the file at that config position. Its text is the encoded position only.
FILE_ACCESS_DENIED = _d(
    "file_access_denied",
    "The file-access policy does not allow this file for this job.",
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
    since="1.2.0",
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
REASON_RECORD_INVALID = _d(
    "reason_record_invalid",
    "A reason record failed validation and was replaced.",
    retry=RetryClass.NEVER,
)
ISOLATED_RUN_TIMED_OUT = _d(
    "isolated_run_timed_out",
    "The isolated worker exceeded its time limit and was stopped.",
    retry=RetryClass.UNKNOWN,
)
ISOLATED_WORKER_DIED = _d(
    "isolated_worker_died",
    "The isolated worker exited without reporting a result.",
    retry=RetryClass.UNKNOWN,
)
ISOLATED_WORKER_OUT_OF_MEMORY = _d(
    "isolated_worker_out_of_memory",
    "The isolated worker ran out of memory.",
    retry=RetryClass.ENVIRONMENT_REPAIR,
)
ROW_ERRORS_FAILED = _d(
    "row_errors_failed", "Row-level strategy errors failed the run.", retry=RetryClass.NEVER
)
CONNECTOR_TRANSIENT_FAILURE = _d(
    "connector_transient_failure",
    "A connector reported a temporary failure.",
    retry=RetryClass.RETRY_OPERATION,
)
CONNECTOR_PERMANENT_FAILURE = _d(
    "connector_permanent_failure",
    "A connector reported a failure that retrying cannot resolve.",
    retry=RetryClass.ENVIRONMENT_REPAIR,
)
RUN_PAUSED = _d(
    "run_paused",
    "The run paused for review and did not fail.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.CANCELLED,
    retry=RetryClass.NEVER,
)
UNSUPPORTED_SCHEMA_VERSION = _d(
    "unsupported_schema_version",
    "A record uses a schema version this reader does not support.",
    retry=RetryClass.NEVER,
)

# F22 output publication (`decoy_engine.publication`, docs/publication.md): the outcomes of
# judging and binding an output unit under its destination lock, and the publishing sink's
# refusals. Each text is fixed; no path, table name or OS message rides with a code.
_p = _catalog.decoy("decoy_engine.publication")

PUBLICATION_LOCK_UNAVAILABLE = _p(
    "publication_lock_unavailable",
    "The output destination cannot be locked on this filesystem.",
    retry=RetryClass.ENVIRONMENT_REPAIR,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_LOCK_TIMEOUT = _p(
    "publication_lock_timeout",
    "Another writer held the output destination lock past the deadline.",
    retry=RetryClass.RETRY_OPERATION,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_TARGET_OCCUPIED = _p(
    "publication_target_occupied",
    "The output destination already holds a different or unreadable result.",
    retry=RetryClass.ENVIRONMENT_REPAIR,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_OPERATION_CONFLICT = _p(
    "publication_operation_conflict",
    "This operation already published a different result to the destination.",
    retry=RetryClass.NEVER,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_COMMIT_UNCERTAIN = _p(
    "publication_commit_uncertain",
    "The output is visible but its durability could not be confirmed.",
    retry=RetryClass.RETRY_OPERATION,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_COMMIT_NOT_VISIBLE = _p(
    "publication_commit_not_visible",
    "An output whose commit was uncertain is not at the destination.",
    retry=RetryClass.RESTART_ATTEMPT,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_VAULT_MISMATCH = _p(
    "publication_vault_mismatch",
    "The committed output at the destination was bound with a different vault.",
    retry=RetryClass.NEVER,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_VAULT_CONFLICT = _p(
    "publication_vault_conflict",
    "A vault with different contents already exists for this output.",
    retry=RetryClass.NEVER,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_WRITE_IN_PROGRESS = _p(
    "publication_write_in_progress",
    "A write is already in progress on this publication.",
    retry=RetryClass.RETRY_OPERATION,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_COMMIT_IN_PROGRESS = _p(
    "publication_commit_in_progress",
    "A commit is already in progress on this publication.",
    retry=RetryClass.RETRY_OPERATION,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_RECOVERY_IN_PROGRESS = _p(
    "publication_recovery_in_progress",
    "A recovery is already in progress on this publication.",
    retry=RetryClass.RETRY_OPERATION,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_ATTEMPT_SEALED = _p(
    "publication_attempt_sealed",
    "This publication attempt already committed and accepts no further change.",
    retry=RetryClass.NEVER,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
PUBLICATION_ATTEMPT_ABORTED = _p(
    "publication_attempt_aborted",
    "This publication attempt was aborted.",
    retry=RetryClass.NEVER,
    outcome=Outcome.PUBLICATION,
    since="1.24.0",
)
