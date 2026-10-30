"""How a failed run's protected-quarantine cleanup ended, and the safe way to report it.

A sealed artifact holds encrypted raw rows, so a run that fails after the seal must either remove
it or keep a handle that lets an operator reconcile exactly that artifact. Pattern source: the
S3 multipart-upload abort and orphan-reconciliation model, where a failed abort never forgets the
upload id. The handle here is value-free: the opaque ref, run identity, key version and expiry
(the retention period is the backstop that ``purge_expired`` enforces).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

NOT_NEEDED = "not_needed"
CONFIRMED = "confirmed"
FAILED = "failed"


@dataclass(frozen=True)
class CleanupOutcome:
    """The end state of ``DeclaredCastRun.abort``; ``ref`` is set only when an artifact remains."""

    cleanup_state: str
    ref: str | None = None
    run_id: str = ""
    key_version: str = ""
    expires_at: float | None = None
    reason: Any = None  # the registered ``protected_quarantine_cleanup_failed`` ReasonRecord


def report_cleanup_failure(
    failing: BaseException | None, outcome: CleanupOutcome
) -> CleanupOutcome:
    """Surface an unfinished cleanup as registered F29 evidence without touching the run's failure.

    The failing exception's own record gains ``cleanup_state=failed`` and the run identity, a
    secondary ``protected_quarantine_cleanup_failed`` is captured in the run's failure scope, and
    the outcome rides on the exception for in-process callers. Evidence is best effort: it never
    masks the failure it describes. Returns the outcome with its registered reason attached.
    """
    try:
        from decoy_engine import reasons
        from decoy_engine.execution._errors import QuarantineError
        from decoy_engine.reason_adapter import amend_failure, capture_failure
        from decoy_engine.reasons import (
            CleanupState,
            Operation,
            Phase,
            ReasonContext,
            ReasonRecord,
            approve_ref,
        )

        context = ReasonContext.build(
            operation=Operation.ABORT,
            storage_ref="quarantine",
            object_ref=approve_ref(outcome.run_id),
            cleanup_state=CleanupState.FAILED,
        )
        code = reasons.runtime.PROTECTED_QUARANTINE_CLEANUP_FAILED
        outcome = replace(
            outcome, reason=ReasonRecord.for_reason(code, phase=Phase.CLEANUP, context=context)
        )
        if failing is not None:
            if amend_failure(failing, context) is None:
                capture_failure(failing, phase=Phase.FINALIZE, context=context)
            _attach(failing, outcome)
        capture_failure(
            QuarantineError(
                code=reasons.runtime.PROTECTED_QUARANTINE_CLEANUP_FAILED, cleanup_state=FAILED
            ),
            phase=Phase.CLEANUP,
            context=context,
            secondary=True,
            reason=code,
        )
    except Exception:
        pass
    return outcome


def _attach(failing: BaseException, outcome: CleanupOutcome) -> None:
    attach: Any = failing
    try:
        attach.declared_cast_cleanup = outcome
    except Exception:  # a slotted or hostile exception simply does not carry it
        pass
