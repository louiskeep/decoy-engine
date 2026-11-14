"""Structured reasons for the isolated-run primitive (F29).

Kept apart from `_isolated_run` (the spawn/classify orchestrator) so that module stays near its
size budget. Both helpers only *build* a `ReasonRecord`; nothing here changes how a run is
spawned, committed or classified.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from decoy_engine.execution._isolated_common import (
    IsolatedRunOutcome,
    IsolatedRunResult,
    exception_error_text,
    is_memory_failure,
    peak_rss_mb,
)
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.reasons import (
    CleanupState,
    Operation,
    Phase,
    PublicationState,
    ReasonContext,
    ReasonRecord,
)
from decoy_engine.reasons import decoy as _decoy
from decoy_engine.verification.postmask import PostScanBlockedError

__all__ = [
    "commit_reason",
    "envelope_failure",
    "in_process_failure",
    "reason_from_envelope",
]


def _blocked_metrics(metrics: Any) -> dict[str, Any]:
    """The only result a blocked run keeps: its post-scan report, and only when it is a mapping.
    The envelope is untrusted input, so a wrong-typed field is dropped, never repaired."""
    report = metrics.get("post_scan_integrity") if isinstance(metrics, dict) else None
    return {"post_scan_integrity": dict(report)} if isinstance(report, dict) else {}


def in_process_failure(exc: BaseException) -> IsolatedRunResult:
    """A run that raised in this process. A post-scan block is its own outcome and keeps the
    report; every other failure is a memory kill or a crash with the safe text and reason."""
    outcome: IsolatedRunOutcome = "oom_killed" if is_memory_failure(exc) else "crashed"
    metrics: dict[str, Any] = {}
    if isinstance(exc, PostScanBlockedError):
        outcome, metrics = "blocked", _blocked_metrics({"post_scan_integrity": exc.report})
    return IsolatedRunResult(
        outcome=outcome,
        peak_rss_mb=round(peak_rss_mb(), 1),
        outputs=None,
        quality_metrics=metrics,
        table_kinds={},
        returncode=None,
        signal_number=None,
        error=exception_error_text(exc),
        isolated=False,
        reason=reason_from_exception(exc),
    )


def envelope_failure(envelope: dict[str, Any], *, pid: int) -> IsolatedRunResult:
    """The result of a child that reported a failure. A `blocked` child also hands back its
    report."""
    blocked = envelope["outcome"] == "blocked"
    return IsolatedRunResult(
        outcome=envelope["outcome"],
        peak_rss_mb=envelope.get("peak_rss_mb"),
        outputs=None,
        quality_metrics=_blocked_metrics(envelope.get("quality_metrics")) if blocked else {},
        table_kinds={},
        returncode=0,
        signal_number=None,
        error=envelope.get("error"),
        isolated=True,
        pid=pid,
        reason=reason_from_envelope(envelope),
    )


def reason_from_envelope(envelope: dict[str, Any]) -> ReasonRecord:
    """The worker's reason, validated here: identity, phase and context are read from the
    envelope, while severity, outcome and retry class are recomputed from this process's
    registry. An old worker (no `reason` key) or a corrupt or newer payload degrades to a fixed
    diagnostic and never to success or to retry advice the registry did not give."""
    if "reason" not in envelope:
        return ReasonRecord.for_reason(
            _decoy.ISOLATED_WORKER_OUT_OF_MEMORY
            if envelope.get("outcome") == "oom_killed"
            else _decoy.UNEXPECTED_ERROR
        )
    return ReasonRecord.from_payload(envelope["reason"])


def commit_reason(exc: BaseException, staging: Path) -> ReasonRecord:
    """A failed atomic commit: nothing was published, and cleanup is confirmed by looking."""
    return reason_from_exception(
        exc,
        phase=Phase.PUBLISH,
        context=ReasonContext.build(
            operation=Operation.COMMIT,
            storage_ref="output",
            publication_state=PublicationState.NOT_PUBLISHED,
            cleanup_state=CleanupState.FAILED if staging.exists() else CleanupState.CONFIRMED,
        ),
    )
