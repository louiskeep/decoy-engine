"""Resume reasons of `decoy run --resume`: why a continuation refused, and what it repaired.

Refusals are usage failures the caller fixes in the command or the project, so they carry the
configuration outcome. They name a field, never a value. The two non-failures ride on a run that
goes on: a kept table that no longer verifies is rerun (a decision), and a store that could not
be removed after delivery is only a warning.
"""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import UNRELEASED, Kind, Outcome, ReasonCode, RetryClass, Severity

__all__ = [
    "RESUME_CHECKPOINT_INVALID",
    "RESUME_FINGERPRINT_CHANGED",
    "RESUME_IN_PROGRESS",
    "RESUME_LAYOUT_UNSUPPORTED",
    "RESUME_NO_CHECKPOINT",
    "RESUME_PLATFORM_UNSUPPORTED",
    "RESUME_ROUTE_UNSUPPORTED",
    "RESUME_RUN_DIR_INVALID",
    "RESUME_SOURCE_CHANGED",
    "RESUME_STORE_INVALID",
    "RESUME_STORE_NOT_REMOVED",
    "RESUME_SUPERSEDED",
    "RESUME_VAULT_UNSUPPORTED",
]

_d = _catalog.decoy("DecoyResume")


def _refusal(code: str, summary: str) -> ReasonCode:
    """A usage refusal: failure, error, configuration, never retried as is."""
    return _d(
        code,
        summary,
        kind=Kind.FAILURE,
        severity=Severity.ERROR,
        outcome=Outcome.CONFIGURATION,
        retry=RetryClass.NEVER,
        since=UNRELEASED,
    )


RESUME_RUN_DIR_INVALID = _refusal(
    "resume_run_dir_invalid",
    "The resume directory is not a run folder of this pipeline.",
)
RESUME_LAYOUT_UNSUPPORTED = _refusal(
    "resume_layout_unsupported",
    "This output layout cannot be resumed.",
)
RESUME_ROUTE_UNSUPPORTED = _refusal(
    "resume_route_unsupported",
    "Only a chunked run can be resumed.",
)
RESUME_VAULT_UNSUPPORTED = _refusal(
    "resume_vault_unsupported",
    "A run with a vault cannot be resumed.",
)
RESUME_PLATFORM_UNSUPPORTED = _refusal(
    "resume_platform_unsupported",
    "This platform cannot lock a resume store, so the run cannot be resumed.",
)
RESUME_NO_CHECKPOINT = _refusal(
    "resume_no_checkpoint",
    "The run has no resume store.",
)
RESUME_STORE_INVALID = _refusal(
    "resume_store_invalid",
    "The resume store is unreadable or malformed.",
)
RESUME_SUPERSEDED = _refusal(
    "resume_superseded",
    "A later run was delivered, so this run cannot be resumed.",
)
RESUME_FINGERPRINT_CHANGED = _refusal(
    "resume_fingerprint_changed",
    "The run differs from the one that wrote the resume store.",
)
RESUME_SOURCE_CHANGED = _refusal(
    "resume_source_changed",
    "A source of a finished table changed since it was run.",
)
# Another process holds the store; waiting and asking again can succeed.
RESUME_IN_PROGRESS = _d(
    "resume_in_progress",
    "Another process is running or resuming this run.",
    kind=Kind.FAILURE,
    severity=Severity.ERROR,
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.RETRY_OPERATION,
    since=UNRELEASED,
)
# A kept table failed verification; the run reruns it instead of refusing.
RESUME_CHECKPOINT_INVALID = _d(
    "resume_checkpoint_invalid",
    "A finished table failed verification and runs again.",
    kind=Kind.DECISION,
    severity=Severity.WARNING,
    outcome=Outcome.PUBLICATION,
    retry=RetryClass.NEVER,
    since=UNRELEASED,
)
RESUME_STORE_NOT_REMOVED = _d(
    "resume_store_not_removed",
    "The resume store was not removed after delivery.",
    kind=Kind.WARNING,
    severity=Severity.WARNING,
    outcome=Outcome.PUBLICATION,
    retry=RetryClass.NEVER,
    since=UNRELEASED,
)
