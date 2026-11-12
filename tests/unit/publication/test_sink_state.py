"""`test_sink_transition_cells`, table half: every cell of both plan 5.6 tables on the state
machine itself (`publication/_sink_state.py`; class 3).

The expected cells below are copied from the plan text, not from the module's table. Each
in-flight state is reached by a call parked inside its I/O on another thread (a hook, never a
sleep), and the arriving call is made while it is parked: it must answer at once, which shows
the transition lock is not held across the I/O. Each case asserts the arriving result, the final
state, and whether the caller is told to abort its stage.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.publication import CommitReceipt, PublicationRefused
from decoy_engine.publication._sink_state import (
    ARRIVALS,
    EXITS,
    Act,
    Exit,
    SinkState,
    SinkStateMachine,
)

S = SinkState
OP = "op-1"
R = object()  # the sealed result a SEALED or UNCERTAIN machine hands back
CALLS = ("write", "commit", "reconcile", "abort")
IN_FLIGHT = (S.WRITING, S.COMMITTING, S.RECOVERING)

# Plan 5.6, first table: (state, call) -> expected arrival.
_WIP = ("busy", "publication_write_in_progress")
_CIP = ("busy", "publication_commit_in_progress")
_RIP = ("busy", "publication_recovery_in_progress")
_ENDED = ("refuse", "ended")  # the code the attempt ended with
EXPECTED: dict[tuple[SinkState, str], tuple[Any, ...]] = {
    (S.OPEN, "write"): ("start", S.WRITING),
    (S.OPEN, "commit"): ("start", S.COMMITTING),
    (S.OPEN, "reconcile"): ("none",),
    (S.OPEN, "abort"): ("abort",),
    (S.WRITING, "write"): _WIP,
    (S.WRITING, "commit"): _WIP,
    (S.WRITING, "reconcile"): _WIP,
    (S.WRITING, "abort"): ("pending",),
    (S.COMMITTING, "write"): _CIP,
    (S.COMMITTING, "commit"): _CIP,
    (S.COMMITTING, "reconcile"): _CIP,
    (S.COMMITTING, "abort"): ("pending",),
    (S.RECOVERING, "write"): _RIP,
    (S.RECOVERING, "commit"): _RIP,
    (S.RECOVERING, "reconcile"): _RIP,
    (S.RECOVERING, "abort"): ("pending",),
    (S.SEALED, "write"): ("refuse", "publication_attempt_sealed"),
    (S.SEALED, "commit"): ("result",),
    (S.SEALED, "reconcile"): ("result",),
    (S.SEALED, "abort"): ("none",),
    (S.UNCERTAIN, "write"): ("refuse", "publication_commit_uncertain"),
    (S.UNCERTAIN, "commit"): ("refuse", "publication_commit_uncertain"),
    (S.UNCERTAIN, "reconcile"): ("start", S.RECOVERING),
    (S.UNCERTAIN, "abort"): ("none",),
    (S.REFUSED, "write"): _ENDED,
    (S.REFUSED, "commit"): _ENDED,
    (S.REFUSED, "reconcile"): _ENDED,
    (S.REFUSED, "abort"): ("abort",),
    (S.FAILED, "write"): _ENDED,
    (S.FAILED, "commit"): _ENDED,
    (S.FAILED, "reconcile"): _ENDED,
    (S.FAILED, "abort"): ("abort",),
    (S.ABORTED, "write"): ("refuse", "publication_attempt_aborted"),
    (S.ABORTED, "commit"): ("refuse", "publication_attempt_aborted"),
    (S.ABORTED, "reconcile"): ("refuse", "publication_attempt_aborted"),
    (S.ABORTED, "abort"): ("none",),
}

# Plan 5.6, second table: (in-flight state, exit) -> final state (and the refusal code it keeps).
EXPECTED_EXITS: dict[tuple[SinkState, Exit], tuple[SinkState, str | None]] = {
    (S.WRITING, Exit.OK): (S.OPEN, None),
    (S.WRITING, Exit.ERROR): (S.FAILED, "publication_failed"),
    (S.COMMITTING, Exit.PUBLISHED): (S.SEALED, None),
    (S.COMMITTING, Exit.ALREADY_COMMITTED): (S.SEALED, None),
    (S.COMMITTING, Exit.OPERATION_CONFLICT): (S.REFUSED, "publication_operation_conflict"),
    (S.COMMITTING, Exit.REFUSED): (S.REFUSED, "publication_target_occupied"),
    (S.COMMITTING, Exit.UNCERTAIN): (S.UNCERTAIN, None),
    (S.COMMITTING, Exit.ERROR): (S.FAILED, "publication_failed"),
    (S.RECOVERING, Exit.FOUND): (S.SEALED, None),
    (S.RECOVERING, Exit.FOUND_NOT_DURABLE): (S.UNCERTAIN, None),
    (S.RECOVERING, Exit.NOT_VISIBLE): (S.REFUSED, "publication_commit_not_visible"),
}

# The code an exit's caller hands over (a refusal receipt's code); the table may override it.
_CALLER_CODE = {
    Exit.OPERATION_CONFLICT: reasons.decoy.PUBLICATION_OPERATION_CONFLICT,
    Exit.REFUSED: reasons.decoy.PUBLICATION_TARGET_OCCUPIED,
}

# How each state is reached through the machine's own calls: a list of steps.
_REACH: dict[SinkState, list[tuple[str, Any]]] = {
    S.OPEN: [],
    S.WRITING: [("arrive", "write")],
    S.COMMITTING: [("arrive", "commit")],
    S.RECOVERING: [("arrive", "commit"), ("leave", Exit.UNCERTAIN), ("arrive", "reconcile")],
    S.SEALED: [("arrive", "commit"), ("leave", Exit.PUBLISHED)],
    S.UNCERTAIN: [("arrive", "commit"), ("leave", Exit.UNCERTAIN)],
    S.REFUSED: [("arrive", "commit"), ("leave", Exit.REFUSED)],
    S.FAILED: [("arrive", "write"), ("leave", Exit.ERROR)],
    S.ABORTED: [("arrive", "abort")],
}


def _reach(state: SinkState) -> SinkStateMachine:
    machine = SinkStateMachine(OP)
    for step, arg in _REACH[state]:
        if step == "arrive":
            machine.arrive(arg)
        else:
            machine.leave(arg, result=R, code=_CALLER_CODE.get(arg))
    assert machine.state is state
    return machine


class _Parked:
    """A call parked inside its I/O on another thread: entered, then held until released."""

    def __init__(self, machine: SinkStateMachine, call: str, exit_: Exit) -> None:
        self.machine = machine
        self.entered = threading.Event()
        self.release = threading.Event()
        self.aborted: bool | None = None
        self.lock_free_during_io = False
        self._exit = exit_
        self._thread = threading.Thread(target=self._run, args=(call,), daemon=True)
        self._thread.start()
        assert self.entered.wait(10)  # non-vacuity: the hook fired with the call in flight

    def _run(self, call: str) -> None:
        if call == "reconcile":
            self.machine.arrive("commit")
            self.machine.leave(Exit.UNCERTAIN, result=R)
        self.machine.arrive(call)
        self.lock_free_during_io = _lock_is_free(self.machine)
        self.entered.set()
        self.release.wait(10)
        self.aborted = self.machine.leave(self._exit, result=R, code=_CALLER_CODE.get(self._exit))

    def finish(self) -> bool | None:
        self.release.set()
        self._thread.join(10)
        assert not self._thread.is_alive()
        return self.aborted


def _lock_is_free(machine: SinkStateMachine) -> bool:
    """True when another thread can take the transition lock right now."""
    got: list[bool] = []

    def probe() -> None:
        lock = machine._lock
        ok = lock.acquire(blocking=False)
        if ok:
            lock.release()
        got.append(ok)

    t = threading.Thread(target=probe)
    t.start()
    t.join(10)
    return got == [True]


_PARK_CALL = {S.WRITING: "write", S.COMMITTING: "commit", S.RECOVERING: "reconcile"}
_OK_EXIT = {S.WRITING: Exit.OK, S.COMMITTING: Exit.PUBLISHED, S.RECOVERING: Exit.FOUND}


def _arrive(machine: SinkStateMachine, call: str) -> tuple[Any, ...]:
    try:
        arrival = machine.arrive(call)
    except PublicationRefused as exc:
        receipt = exc.receipt
        assert type(receipt) is CommitReceipt
        assert (receipt.outcome, receipt.operation_id, receipt.reason_code) == (
            "refused",
            OP,
            exc.code,
        )
        assert str(exc) == f"publication refused: {exc.code}"
        return ("raised", exc.code)
    if arrival.act is Act.START:
        return ("start", machine.state)
    if arrival.act is Act.RESULT:
        assert arrival.result is R
        return ("result",)
    assert arrival.result is None
    return ({Act.NOTHING: "none", Act.PENDING: "pending", Act.ABORT: "abort"}[arrival.act],)


def _ended_code(state: SinkState) -> str:
    return {S.REFUSED: "publication_target_occupied", S.FAILED: "publication_failed"}[state]


@pytest.mark.parametrize(("state", "call"), sorted(EXPECTED, key=lambda k: (k[0].value, k[1])))
def test_sink_transition_cells_arrival(state: SinkState, call: str) -> None:
    expected = EXPECTED[(state, call)]
    parked = None
    if state in IN_FLIGHT:
        parked = _Parked(SinkStateMachine(OP), _PARK_CALL[state], _OK_EXIT[state])
        machine = parked.machine
        assert machine.state is state
        assert parked.lock_free_during_io
    else:
        machine = _reach(state)
    got = _arrive(machine, call)
    kind = expected[0]
    if kind in ("busy", "refuse"):
        code = _ended_code(state) if expected[1] == "ended" else expected[1]
        assert got == ("raised", code)
        assert machine.state is state  # a refusal changes nothing
    elif kind == "start":
        assert got == expected
    elif kind == "abort":
        assert got == ("abort",)
        assert machine.state is S.ABORTED
    else:
        assert got == expected
        assert machine.state is state
    if parked is not None:
        aborted = parked.finish()
        assert aborted is (kind == "pending" and state is S.WRITING)
        # A pending abort applies on the exit unless that exit seals (OK from WRITING is OPEN).
        final = S.ABORTED if aborted else {S.WRITING: S.OPEN}.get(state, S.SEALED)
        assert machine.state is final


@pytest.mark.parametrize(
    ("origin", "exit_"), sorted(EXPECTED_EXITS, key=lambda k: (k[0].value, k[1].value))
)
@pytest.mark.parametrize("pending_abort", [False, True])
def test_sink_transition_cells_exit(origin: SinkState, exit_: Exit, pending_abort: bool) -> None:
    final, code = EXPECTED_EXITS[(origin, exit_)]
    parked = _Parked(SinkStateMachine(OP), _PARK_CALL[origin], exit_)
    machine = parked.machine
    assert parked.lock_free_during_io
    if pending_abort:
        assert _arrive(machine, "abort") == ("pending",)
        assert machine.state is origin  # the abort waits for the exit
    aborted = parked.finish()
    keeps = final in (S.SEALED, S.UNCERTAIN)
    assert aborted is (pending_abort and not keeps)
    assert machine.state is (S.ABORTED if aborted else final)
    if aborted:
        assert _arrive(machine, "write") == ("raised", "publication_attempt_aborted")
    elif code is not None:
        assert _arrive(machine, "commit") == ("raised", code)
    elif final in (S.SEALED,):
        assert machine.arrive("commit").result is R


def test_sink_transition_cells_tables_are_complete_and_literal() -> None:
    assert set(ARRIVALS) == {(s, c) for s in SinkState for c in CALLS}
    assert set(EXITS) == set(EXPECTED_EXITS)
    for (state, call), cell in ARRIVALS.items():
        expected = EXPECTED[(state, call)]
        assert cell.act.value == {"none": "nothing"}.get(expected[0], expected[0])
        if expected[0] == "start":
            assert cell.state is expected[1]


def test_sink_transition_cells_a_leave_from_a_settled_state_is_a_programming_error() -> None:
    machine = SinkStateMachine(OP)
    with pytest.raises(KeyError):
        machine.leave(Exit.OK)
    assert machine.state is S.OPEN


def test_sink_transition_cells_reject_an_invalid_operation_id() -> None:
    with pytest.raises(ValueError, match="operation_id"):
        SinkStateMachine("op\n")


def test_sink_transition_cells_concurrent_arrivals_start_one_call_only() -> None:
    machine = SinkStateMachine(OP)
    barrier = threading.Barrier(8)
    outcomes: list[str] = []
    guard = threading.Lock()

    def racer(call: Callable[[], Any]) -> None:
        barrier.wait(10)
        try:
            call()
            got = "started"
        except PublicationRefused as exc:
            got = exc.code
        with guard:
            outcomes.append(got)

    threads = [
        threading.Thread(target=racer, args=(lambda: machine.arrive("write"),)) for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(outcomes) == ["publication_write_in_progress"] * 7 + ["started"]
    assert machine.state is S.WRITING
