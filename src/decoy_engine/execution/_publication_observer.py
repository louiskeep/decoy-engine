"""Publication evidence: what the engine can truthfully say about an output at each boundary.

F29 reports a publication outcome; it does not change how output is published. A sink's write,
close, commit and abort boundaries are observed here, and the evidence is attached to the failure
record (`ReasonContext`) so a consumer can tell "nothing was published and cleanup is confirmed"
from "the tables are committed and only the sidecar failed" from "the sink gave no evidence".

Three rules keep the report truthful:

* A sink that keeps evidence (the two engine sinks) reports it. A sink that does not is reported
  as ``unknown``, never as clean: its ``abort()`` returning proves nothing about cleanup.
* ``commit()`` returning is the only evidence of ``published``. A failed atomic rename left
  nothing published (the reference sink's contract); a failed commit on a sink with no such
  contract is ``unknown``. The reference sink projects its state-table state onto its evidence
  (``project_sink_state``): sealed is published, uncertain (bound, not confirmed durable) is
  unknown, refused, failed and aborted are not published.
* ``abort()`` is best effort and its failure never masks the run's first failure; its outcome is
  recorded as cleanup state, ``confirmed`` only when the sink checked.

Pattern: the transactional-outbox / two-phase publish vocabulary (stage, commit, abort) already
used by `_transactional_sink`; this module adds observation only, no new publication algorithm.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from decoy_engine import reasons
from decoy_engine.publication import CommitReceipt
from decoy_engine.publication._sink_state import SinkState
from decoy_engine.reason_adapter import amend_failure, capture_failure
from decoy_engine.reasons import (
    CleanupState,
    Operation,
    Phase,
    PublicationState,
    ReasonContext,
    ReasonRecord,
)

STORAGE_REF = "output"
"""The approved storage alias reported instead of any path or URI."""


@dataclass
class PublicationEvidence:
    """Mutable evidence one sink keeps about its own publication."""

    state: PublicationState = PublicationState.NOT_PUBLISHED
    cleanup: CleanupState = CleanupState.NOT_NEEDED
    last_operation: Operation | None = None
    stored_bytes: dict[str, int] = field(default_factory=dict)

    def context(
        self, operation: Operation | None = None, *, table: str | None = None
    ) -> ReasonContext:
        from decoy_engine.reasons import approve_ref

        return ReasonContext.build(
            operation=operation or self.last_operation,
            storage_ref=STORAGE_REF,
            publication_state=self.state,
            cleanup_state=self.cleanup,
            object_ref=approve_ref(table) if table else None,
        )


_P, _C = PublicationState, CleanupState
SINK_STATE_EVIDENCE: Final[Mapping[SinkState, tuple[PublicationState, CleanupState | None]]] = (
    MappingProxyType(
        {
            SinkState.OPEN: (_P.NOT_PUBLISHED, None),
            SinkState.WRITING: (_P.NOT_PUBLISHED, None),
            SinkState.COMMITTING: (_P.NOT_PUBLISHED, None),
            SinkState.RECOVERING: (_P.UNKNOWN, _C.NOT_NEEDED),
            SinkState.SEALED: (_P.PUBLISHED, _C.NOT_NEEDED),
            SinkState.UNCERTAIN: (_P.UNKNOWN, _C.NOT_NEEDED),
            SinkState.REFUSED: (_P.NOT_PUBLISHED, None),
            SinkState.FAILED: (_P.NOT_PUBLISHED, None),
            SinkState.ABORTED: (_P.NOT_PUBLISHED, None),
        }
    )
)
"""Sink state to F29 evidence (plan 5.6). A cleanup of ``None`` keeps what the sink observed
about its stage; a bound unit (sealed, uncertain) has no stage left to clean."""


def project_sink_state(evidence: PublicationEvidence, state: SinkState) -> None:
    """Set ``evidence`` from the sink's state-table ``state``."""
    publication, cleanup = SINK_STATE_EVIDENCE[state]
    evidence.state = publication
    if cleanup is not None:
        evidence.cleanup = cleanup


def evidence_of(sink: Any) -> PublicationEvidence | None:
    """The sink's own evidence, or ``None`` for a sink that keeps none."""
    evidence = getattr(sink, "publication_evidence", None)
    return evidence if isinstance(evidence, PublicationEvidence) else None


def _unknown(operation: Operation) -> ReasonContext:
    return ReasonContext.build(
        operation=operation,
        storage_ref=STORAGE_REF,
        publication_state=PublicationState.UNKNOWN,
        cleanup_state=CleanupState.UNKNOWN,
    )


def capture_publication_failure(
    exc: BaseException,
    sink: Any,
    operation: Operation,
    *,
    table: str | None = None,
    secondary: bool = False,
) -> ReasonRecord | None:
    """Record ``exc`` as a failure at ``operation`` with the sink's evidence (or ``unknown``).

    Observation never masks the failure it observes: any error while recording is swallowed."""
    try:
        evidence = evidence_of(sink)
        context = evidence.context(operation, table=table) if evidence else _unknown(operation)
        return capture_failure(exc, phase=Phase.PUBLISH, context=context, secondary=secondary)
    except Exception:
        return None


def observed_commit(sink: Any) -> CommitReceipt | None:
    """``sink.commit()``, recording the outcome; returns the sink's ``CommitReceipt`` (the
    value ``commit()`` returned, else the sink's ``receipt``), or ``None`` for a sink that
    gives none. Exceptions propagate unchanged."""
    evidence = evidence_of(sink)
    try:
        returned = sink.commit()
    except BaseException as exc:
        # An engine sink has already recorded its own failed-commit state; a sink with no
        # evidence is reported ``unknown``.
        capture_publication_failure(exc, sink, Operation.COMMIT)
        raise
    if evidence is not None:
        evidence.state = PublicationState.PUBLISHED
        evidence.last_operation = Operation.COMMIT
    receipt = returned if isinstance(returned, CommitReceipt) else getattr(sink, "receipt", None)
    return receipt if isinstance(receipt, CommitReceipt) else None


def observed_abort(sink: Any, failing: BaseException | None = None) -> None:
    """``sink.abort()``, best effort exactly as before: an ``Exception`` is suppressed (it must
    not mask the run's own failure), a ``BaseException`` such as ``KeyboardInterrupt`` is not.

    The cleanup outcome is recorded on the sink's evidence and amended onto ``failing``'s record,
    so the failure a consumer reads reflects what happened *after* the failure. A failed abort is
    a secondary failure and never replaces the primary one."""
    evidence = evidence_of(sink)
    if evidence is not None:
        evidence.last_operation = Operation.ABORT
    try:
        sink.abort()
    except Exception as exc:
        if evidence is not None:
            evidence.cleanup = CleanupState.FAILED
        capture_publication_failure(exc, sink, Operation.ABORT, secondary=True)
        state = CleanupState.FAILED
    else:
        # A sink with evidence checks and sets `confirmed` itself; one without gets `attempted`:
        # the call returning proves nothing about cleanup.
        state = evidence.cleanup if evidence is not None else CleanupState.ATTEMPTED
    if failing is not None:
        try:
            amend_failure(failing, ReasonContext.build(cleanup_state=state))
        except Exception:  # evidence is best effort; it never masks the failure it describes
            pass


def report_partial_publication(failure: BaseException) -> ReasonRecord | None:
    """A failure after the tables committed (a quarantine sidecar): the output is partly published.

    The committed tables are never aborted; the failure is reported as ``publication_partial``
    with no replay advice, whatever the exception itself looks like. Never masks ``failure``."""
    try:
        context = ReasonContext.build(
            operation=Operation.SIDECAR,
            storage_ref=STORAGE_REF,
            publication_state=PublicationState.PARTIAL,
            cleanup_state=CleanupState.NOT_NEEDED,
        )
        return capture_failure(
            failure, phase=Phase.PUBLISH, context=context, reason=reasons.decoy.PUBLICATION_PARTIAL
        )
    except Exception:
        return None
