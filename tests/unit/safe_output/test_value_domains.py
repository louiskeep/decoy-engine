"""F31 T5: value domains the safe-output slice must not move (characterization first).

Imports only base modules at top level; tests that need decoy_engine.safe_output import it
inside the test, so the characterization cells run on a base without that package.
"""

from __future__ import annotations

import pytest

from decoy_engine import phase, progress, reasons
from decoy_engine.reasons import CleanupState, Operation, PublicationState, ReasonRecord

_RV = reasons.REGISTRY_VERSION
_TS = 1700000000.0


def _events() -> dict[str, progress.ProgressEvent]:
    rec = ReasonRecord.for_reason(reasons.decoy.RUN_PAUSED)
    return {
        "phase": progress.PhaseEvent(
            "mask",
            "error",
            "orders",
            rows_in=10,
            rows_out=9,
            elapsed_s=1.5,
            error_class="ValueError",
            ts=_TS,
            sequence=3,
            phase="mask",
            object_ref="orders",
            bytes_done=100,
            bytes_total_estimate=200,
            byte_basis="arrow_logical",
        ),
        "rate": progress.RateSample(
            "orders", 50, 25.0, 2.0, rows_total_estimate=100, final=False, ts=_TS, sequence=4
        ),
        "route": progress.RouteEvent("arrow", lane="native", table="orders", ts=_TS, sequence=5),
        "warning": progress.WarningEvent(
            "unregistered_reason", table="orders", column="email", ts=_TS, sequence=6
        ),
        "terminal": progress.TerminalEvent(
            "error",
            error_class="Leak7Error",
            elapsed_s=3.0,
            sink_failures=1,
            ts=_TS,
            sequence=7,
            reason=rec,
        ),
        "retry": progress.RetryEvent(
            table="orders",
            ts=_TS,
            sequence=8,
            retry=progress.RetryInfo("attempt", 2, 1, "scheduled", 0.5),
        ),
        "checkpoint": progress.CheckpointEvent(
            table="orders",
            ts=_TS,
            sequence=9,
            checkpoint=progress.CheckpointInfo("committed", "ck1", 2, 10, 300),
            publication=progress.PublicationInfo(
                Operation.COMMIT, "out", PublicationState.PUBLISHED, CleanupState.NOT_NEEDED
            ),
        ),
    }


# (event_to_dict, event_to_legacy_dict) per event type, captured on the pre-F31 base.
_SNAPSHOT = {
    "phase": (
        {
            "byte_basis": "arrow_logical",
            "bytes_done": 100,
            "bytes_total_estimate": 200,
            "checkpoint": None,
            "elapsed_s": 1.5,
            "error_class": "ValueError",
            "event_schema_version": 1,
            "kind": "phase",
            "name": "mask",
            "object_ref": "orders",
            "phase": "mask",
            "publication": None,
            "reason": None,
            "registry_version": _RV,
            "retry": None,
            "rows_in": 10,
            "rows_out": 9,
            "sequence": 3,
            "status": "error",
            "table": "orders",
            "ts": 1700000000.0,
        },
        {
            "elapsed_s": 1.5,
            "error_class": "ValueError",
            "kind": "phase",
            "name": "mask",
            "rows_in": 10,
            "rows_out": 9,
            "status": "error",
            "table": "orders",
            "ts": 1700000000.0,
        },
    ),
    "rate": (
        {
            "byte_basis": None,
            "bytes_done": None,
            "bytes_total_estimate": None,
            "elapsed_s": 2.0,
            "event_schema_version": 1,
            "final": False,
            "kind": "rate",
            "object_ref": None,
            "phase": None,
            "registry_version": _RV,
            "retry": None,
            "rows_done": 50,
            "rows_per_sec": 25.0,
            "rows_total_estimate": 100,
            "sequence": 4,
            "table": "orders",
            "ts": 1700000000.0,
        },
        {
            "elapsed_s": 2.0,
            "final": False,
            "kind": "rate",
            "rows_done": 50,
            "rows_per_sec": 25.0,
            "rows_total_estimate": 100,
            "table": "orders",
            "ts": 1700000000.0,
        },
    ),
    "route": (
        {
            "event_schema_version": 1,
            "kind": "route",
            "lane": "native",
            "object_ref": None,
            "phase": None,
            "reason": None,
            "reason_code": None,
            "registry_version": _RV,
            "route": "arrow",
            "sequence": 5,
            "table": "orders",
            "ts": 1700000000.0,
        },
        {
            "kind": "route",
            "lane": "native",
            "reason_code": None,
            "route": "arrow",
            "table": "orders",
            "ts": 1700000000.0,
        },
    ),
    "warning": (
        {
            "code": "unregistered_reason",
            "column": "email",
            "event_schema_version": 1,
            "kind": "warning",
            "object_ref": None,
            "phase": None,
            "reason": None,
            "registry_version": _RV,
            "sequence": 6,
            "table": "orders",
            "ts": 1700000000.0,
        },
        {
            "code": "unregistered_reason",
            "column": "email",
            "kind": "warning",
            "table": "orders",
            "ts": 1700000000.0,
        },
    ),
    "terminal": (
        {
            "elapsed_s": 3.0,
            "error_class": "Exception",
            "event_schema_version": 1,
            "kind": "terminal",
            "object_ref": None,
            "phase": None,
            "publication": None,
            "reason": {
                "code": "run_paused",
                "context": {
                    "cleanup_state": None,
                    "counts": {},
                    "object_ref": None,
                    "operation": None,
                    "original_identity": None,
                    "publication_state": None,
                    "storage_ref": None,
                    "truncated": False,
                },
                "kind": "control",
                "namespace": "decoy",
                "outcome": "cancelled",
                "phase": "unknown",
                "registry_version": _RV,
                "retry_class": "never",
                "schema_version": 1,
                "severity": "info",
            },
            "registry_version": _RV,
            "sequence": 7,
            "sink_failures": 1,
            "status": "error",
            "ts": 1700000000.0,
        },
        {
            "elapsed_s": 3.0,
            "error_class": "Exception",
            "kind": "terminal",
            "sink_failures": 1,
            "status": "error",
            "ts": 1700000000.0,
        },
    ),
    "retry": (
        {
            "event_schema_version": 1,
            "kind": "retry",
            "object_ref": None,
            "phase": None,
            "reason": None,
            "registry_version": _RV,
            "retry": {
                "attempt": 2,
                "delay_s": 0.5,
                "retries_completed": 1,
                "scope": "attempt",
                "state": "scheduled",
            },
            "sequence": 8,
            "table": "orders",
            "ts": 1700000000.0,
        },
        {"kind": "retry", "table": "orders", "ts": 1700000000.0},
    ),
    "checkpoint": (
        {
            "byte_basis": None,
            "bytes_done": None,
            "checkpoint": {
                "bytes": 300,
                "reference": "ck1",
                "rows": 10,
                "sequence": 2,
                "state": "committed",
            },
            "event_schema_version": 1,
            "kind": "checkpoint",
            "object_ref": None,
            "phase": None,
            "reason": None,
            "registry_version": _RV,
            "sequence": 9,
            "table": "orders",
            "ts": 1700000000.0,
        },
        {"kind": "checkpoint", "table": "orders", "ts": 1700000000.0},
    ),
}


@pytest.mark.parametrize("name", sorted(_SNAPSHOT))
def test_progress_dicts_unchanged(name: str) -> None:
    event = _events()[name]
    assert (progress.event_to_dict(event), progress.event_to_legacy_dict(event)) == _SNAPSHOT[name]


class _PlainSubclassError(ValueError):
    pass


# Today's 15 labels: 14 builtins by exact type, anything else "Exception".
_LABELS: tuple[tuple[type[BaseException], str], ...] = (
    (OSError, "OSError"),
    (ValueError, "ValueError"),
    (TypeError, "TypeError"),
    (KeyError, "KeyError"),
    (IndexError, "IndexError"),
    (RuntimeError, "RuntimeError"),
    (MemoryError, "MemoryError"),
    (TimeoutError, "TimeoutError"),
    (NotImplementedError, "NotImplementedError"),
    (ArithmeticError, "ArithmeticError"),
    (ZeroDivisionError, "ZeroDivisionError"),
    (OverflowError, "OverflowError"),
    (UnicodeError, "UnicodeError"),
    (AssertionError, "AssertionError"),
    (_PlainSubclassError, "Exception"),
)


def _error_type(exc: BaseException) -> object:
    ref = reasons.get_reason("post_scan", "check_error").ref
    res = phase.error_result("post_scan", "integrity", "m", "1", ref, exc)
    return phase.to_wire(res)["payload"]["error_type"]


@pytest.mark.parametrize(
    ("cls", "label"), _LABELS, ids=[label for _, label in _LABELS[:-1]] + ["subclass"]
)
def test_phase_error_type_domain_superset(cls: type[BaseException], label: str) -> None:
    assert _error_type(cls.__new__(cls)) == label


def _families() -> list[type[BaseException]]:
    from decoy_engine.execution._errors import ExecutionError
    from decoy_engine.plan._errors import PlanCompileError
    from decoy_engine.subset._errors import SubsetError

    return [SubsetError, PlanCompileError, ExecutionError]


@pytest.mark.parametrize("cls", _families(), ids=lambda c: c.__name__)
def test_phase_error_type_names_engine_family(cls: type[BaseException]) -> None:
    assert _error_type(cls.__new__(cls)) == cls.__name__


def test_oserror_maps_to_oserror_not_ioerror() -> None:
    from decoy_engine.safe_output import SafeErrorType, safe_error_type

    assert safe_error_type(OSError()).value == "OSError"
    assert {"IOError", "EnvironmentError"} <= {m.value for m in SafeErrorType}
    assert _error_type(OSError()) == "OSError"


def test_label_set_equals_safe_error_class_set() -> None:
    from decoy_engine._progress_events import _safe_error_names
    from decoy_engine.safe_output import SafeErrorType

    assert {m.value for m in SafeErrorType} == _safe_error_names() | {"Exception"}
