"""Structured reasons for the isolated-run primitive (F29).

Kept apart from `_isolated_run` (the spawn/classify orchestrator) so that module stays near its
size budget. Both helpers only *build* a `ReasonRecord`; nothing here changes how a run is
spawned, committed or classified.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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

__all__ = ["commit_reason", "reason_from_envelope"]


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
