"""Sink state table: the states of one publishing sink attempt and the cells between them.

Pattern: a table-driven finite-state machine (the state-transition table of UML and W3C SCXML):
every (state, call) and (in-flight state, exit) pair is one literal cell below, so a missing or
doubled transition is a visible table edit, never a branch. The transition is taken under one
re-entrant lock and the I/O it admits runs outside the lock, as `java.util.concurrent.FutureTask`
changes state under a guard and runs its task outside it: a call that arrives while another is in
flight answers at once (busy, or a pending abort applied on the exit) instead of waiting on I/O.

States and cells are plan 5.6 (F22). Refusals are ``PublicationRefused`` with a registered
``decoy`` code and a value-free receipt; this module does no I/O and knows no sink.
Contract: ``docs/publication.md``.
"""

from __future__ import annotations

import enum
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from decoy_engine import reasons
from decoy_engine.publication._types import CommitReceipt, PublicationRefused, is_operation_id

if TYPE_CHECKING:
    from decoy_engine.reasons import ReasonCode

__all__ = [
    "ARRIVALS",
    "EXITS",
    "Act",
    "Arrival",
    "Call",
    "Cell",
    "Exit",
    "Landing",
    "SinkState",
    "SinkStateMachine",
]

Call = Literal["write", "commit", "reconcile", "abort"]


class SinkState(enum.Enum):
    OPEN = "open"
    WRITING = "writing"
    COMMITTING = "committing"
    RECOVERING = "recovering"
    SEALED = "sealed"
    UNCERTAIN = "uncertain"
    REFUSED = "refused"
    FAILED = "failed"
    ABORTED = "aborted"


class Act(enum.Enum):
    """What an arriving call does."""

    START = "start"  # take the cell's in-flight state; the caller runs the I/O, then leaves
    BUSY = "busy"  # refused: another call is in flight
    REFUSE = "refuse"  # refused: the attempt is over
    RESULT = "result"  # return the sealed result, no I/O
    NOTHING = "nothing"  # return None, no I/O, no change
    PENDING = "pending"  # an abort in flight: recorded, applied on the exit
    ABORT = "abort"  # ABORTED; the caller aborts its stage


class Exit(enum.Enum):
    """How an in-flight call ended."""

    OK = "ok"
    ERROR = "error"  # an exception, or a commit that failed before the rename
    PUBLISHED = "published"
    ALREADY_COMMITTED = "already_committed"
    OPERATION_CONFLICT = "operation_conflict"
    REFUSED = "refused"
    UNCERTAIN = "uncertain"
    FOUND = "found"  # reconcile: bound with this result, its chain fsynced (D4)
    FOUND_NOT_DURABLE = "found_not_durable"  # reconcile: bound with this result, D4 failed
    NOT_VISIBLE = "not_visible"  # reconcile: absent, or a different result


@dataclass(frozen=True)
class Cell:
    act: Act
    state: SinkState | None = None  # START: the in-flight state taken
    code: ReasonCode | None = None  # BUSY, REFUSE; None: the code the attempt ended with


@dataclass(frozen=True)
class Landing:
    state: SinkState
    code: ReasonCode | None = None  # kept for later refusals; None: the caller's code


@dataclass(frozen=True)
class Arrival:
    act: Act
    result: object | None = None  # RESULT only


_S = SinkState
_WIP = Cell(Act.BUSY, code=reasons.decoy.PUBLICATION_WRITE_IN_PROGRESS)
_CIP = Cell(Act.BUSY, code=reasons.decoy.PUBLICATION_COMMIT_IN_PROGRESS)
_RIP = Cell(Act.BUSY, code=reasons.decoy.PUBLICATION_RECOVERY_IN_PROGRESS)
_PENDING = Cell(Act.PENDING)
_ABORT = Cell(Act.ABORT)
_NOTHING = Cell(Act.NOTHING)
_RESULT = Cell(Act.RESULT)
_ENDED = Cell(Act.REFUSE)
_SEALED_REFUSE = Cell(Act.REFUSE, code=reasons.decoy.PUBLICATION_ATTEMPT_SEALED)
_UNCERTAIN_REFUSE = Cell(Act.REFUSE, code=reasons.decoy.PUBLICATION_COMMIT_UNCERTAIN)
_ABORTED_REFUSE = Cell(Act.REFUSE, code=reasons.decoy.PUBLICATION_ATTEMPT_ABORTED)

ARRIVALS: Final[Mapping[tuple[SinkState, Call], Cell]] = MappingProxyType(
    {
        (_S.OPEN, "write"): Cell(Act.START, _S.WRITING),
        (_S.OPEN, "commit"): Cell(Act.START, _S.COMMITTING),
        (_S.OPEN, "reconcile"): _NOTHING,  # seals nothing before a result is measured
        (_S.OPEN, "abort"): _ABORT,
        (_S.WRITING, "write"): _WIP,
        (_S.WRITING, "commit"): _WIP,
        (_S.WRITING, "reconcile"): _WIP,
        (_S.WRITING, "abort"): _PENDING,
        (_S.COMMITTING, "write"): _CIP,
        (_S.COMMITTING, "commit"): _CIP,
        (_S.COMMITTING, "reconcile"): _CIP,
        (_S.COMMITTING, "abort"): _PENDING,
        (_S.RECOVERING, "write"): _RIP,
        (_S.RECOVERING, "commit"): _RIP,
        (_S.RECOVERING, "reconcile"): _RIP,
        (_S.RECOVERING, "abort"): _PENDING,
        (_S.SEALED, "write"): _SEALED_REFUSE,
        (_S.SEALED, "commit"): _RESULT,
        (_S.SEALED, "reconcile"): _RESULT,
        (_S.SEALED, "abort"): _NOTHING,
        (_S.UNCERTAIN, "write"): _UNCERTAIN_REFUSE,
        (_S.UNCERTAIN, "commit"): _UNCERTAIN_REFUSE,
        (_S.UNCERTAIN, "reconcile"): Cell(Act.START, _S.RECOVERING),
        (_S.UNCERTAIN, "abort"): _NOTHING,
        (_S.REFUSED, "write"): _ENDED,
        (_S.REFUSED, "commit"): _ENDED,
        (_S.REFUSED, "reconcile"): _ENDED,
        (_S.REFUSED, "abort"): _ABORT,
        (_S.FAILED, "write"): _ENDED,
        (_S.FAILED, "commit"): _ENDED,
        (_S.FAILED, "reconcile"): _ENDED,
        (_S.FAILED, "abort"): _ABORT,
        (_S.ABORTED, "write"): _ABORTED_REFUSE,
        (_S.ABORTED, "commit"): _ABORTED_REFUSE,
        (_S.ABORTED, "reconcile"): _ABORTED_REFUSE,
        (_S.ABORTED, "abort"): _NOTHING,
    }
)

_FAILED = Landing(_S.FAILED, reasons.decoy.PUBLICATION_FAILED)

EXITS: Final[Mapping[tuple[SinkState, Exit], Landing]] = MappingProxyType(
    {
        (_S.WRITING, Exit.OK): Landing(_S.OPEN),
        (_S.WRITING, Exit.ERROR): _FAILED,
        (_S.COMMITTING, Exit.PUBLISHED): Landing(_S.SEALED),
        (_S.COMMITTING, Exit.ALREADY_COMMITTED): Landing(_S.SEALED),
        (_S.COMMITTING, Exit.OPERATION_CONFLICT): Landing(_S.REFUSED),
        (_S.COMMITTING, Exit.REFUSED): Landing(_S.REFUSED),
        (_S.COMMITTING, Exit.UNCERTAIN): Landing(_S.UNCERTAIN),
        (_S.COMMITTING, Exit.ERROR): _FAILED,
        (_S.RECOVERING, Exit.FOUND): Landing(_S.SEALED),
        (_S.RECOVERING, Exit.FOUND_NOT_DURABLE): Landing(_S.UNCERTAIN),
        (_S.RECOVERING, Exit.NOT_VISIBLE): Landing(
            _S.REFUSED, reasons.decoy.PUBLICATION_COMMIT_NOT_VISIBLE
        ),
    }
)

# A pending abort is dropped on these exits: the result stays bound, and abort there is a no-op.
_KEEPS: Final = frozenset({_S.SEALED, _S.UNCERTAIN})


class SinkStateMachine:
    """One attempt's state, moved only through ``ARRIVALS`` and ``EXITS`` under one lock.

    ``arrive(call)`` takes the cell for the current state: it raises ``PublicationRefused``
    (busy or refused) or returns what the caller does next. After a START the caller runs its
    I/O with no lock held and reports how it ended with ``leave(exit)``, which returns True when
    a pending abort applied and the caller must now abort its stage."""

    def __init__(self, operation_id: str) -> None:
        if not is_operation_id(operation_id):
            raise ValueError("invalid operation_id")
        self.operation_id = operation_id
        self._lock = threading.RLock()
        self._state = SinkState.OPEN
        self._pending_abort = False
        self._result: object | None = None
        self._ended: ReasonCode | None = None

    @property
    def state(self) -> SinkState:
        with self._lock:
            return self._state

    def arrive(self, call: Call) -> Arrival:
        with self._lock:
            cell = ARRIVALS[(self._state, call)]
            if cell.act is Act.START and cell.state is not None:
                self._state = cell.state
            elif cell.act is Act.ABORT:
                self._state = SinkState.ABORTED
            elif cell.act is Act.PENDING:
                self._pending_abort = True
            code = cell.code if cell.code is not None else self._ended
            result = self._result if cell.act is Act.RESULT else None
        if cell.act in (Act.BUSY, Act.REFUSE):
            self._refuse(code or reasons.decoy.PUBLICATION_FAILED)
        return Arrival(cell.act, result)

    def leave(
        self, exit_: Exit, *, result: object | None = None, code: ReasonCode | None = None
    ) -> bool:
        with self._lock:
            landing = EXITS[(self._state, exit_)]
            aborted = self._pending_abort and landing.state not in _KEEPS
            self._pending_abort = False
            self._state = SinkState.ABORTED if aborted else landing.state
            if landing.state in _KEEPS:
                self._result = result
            self._ended = landing.code or code
        return aborted

    def _refuse(self, code: ReasonCode) -> None:
        receipt = CommitReceipt(
            outcome="refused",
            operation_id=self.operation_id,
            reason_code=str(code),
            result_id=None,
            descriptor_digest=None,
            vault_digest=None,
            artifacts=(),
            visible=False,
            durable=False,
            os_error=None,
        )
        raise PublicationRefused(code, receipt)
