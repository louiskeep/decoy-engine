"""V1 threat model: ordinary-shape canaries never reach an error path.

Scope: error paths, not accepted field content. The canaries sit in positions the envelope
rejects (or in a callee's exception), and every `PhaseContractError` raised for them is walked:
`str`, `repr`, `args`, `__notes__`, the whole `__context__`/`__cause__` chain, and
`TracebackException(capture_locals=True)` locals of the frames in `phase.py`; `error_result`
output (its `repr` and `to_wire`) is walked too. Frames outside `phase.py` (the caller's own
frames and the dataclass-generated `__init__`) hold the caller's own arguments and are out of
the engine's reach, so they are not walked. A value in an accepted field (payload, identity,
check_id, ...) is caller-owned evidence passed through unchanged; keeping source values out of
it is the producer's job (F31), and `test_accepted_field_content_*` pins that contract. Hostile
in-process objects are deferred to V2 (`test_v2_hostile_deferred.py`).
"""

from __future__ import annotations

import dataclasses
import json
import traceback
from collections.abc import Callable, Iterator

import pytest

from decoy_engine import phase, reasons
from decoy_engine.reasons import ReasonRef

_MARK = "V1CANARY7f3a"
_PAN = "4111111111111111"
_MARKERS = (_MARK, _PAN)


@dataclasses.dataclass
class _Row:
    """An ordinary caller dataclass; its generated repr carries the marker."""

    value: str


def _result(**kw):
    base = dict(
        phase="post_scan", check_id="integrity", status="passed", method="m", method_version="1"
    )
    base.update(kw)
    return phase.PhaseResult(**base)


def _raises_from_callee(value: str) -> str:
    raise ValueError(f"cell {value} failed")


def _contract_error_from_callee(value: str) -> str:
    # A caller-side helper reusing the engine's error type with the value in the text.
    raise phase.PhaseContractError(f"bad value {value}")


def _scenarios() -> list[tuple[str, Callable[[], object]]]:
    return [
        ("str-status", lambda: phase.to_wire(_result(status=_MARK))),
        ("str-phase", lambda: phase.canonical_bytes(_result(phase=_MARK))),
        ("str-event-status", lambda: phase.to_wire(phase.PhaseEvent("post_scan", "e", _MARK))),
        ("str-event-reason", lambda: phase.PhaseEvent("post_scan", "e", "error", _MARK)),  # type: ignore[arg-type]
        ("int-as-string-rows", lambda: phase.PhaseCoverage(rows_examined=_PAN)),  # type: ignore[arg-type]
        ("int-as-string-check-id", lambda: _result(check_id=int(_PAN))),
        ("list-scope", lambda: phase.PhaseCoverage(requested_scope=[_MARK, 7])),  # type: ignore[list-item]
        ("list-identity", lambda: _result(identity=[(_MARK, 1)])),
        ("dict-int-key", lambda: phase.freeze({1: _MARK})),
        ("dict-nan", lambda: _result(payload={"k": {_MARK: float("nan")}})),
        ("dataclass-freeze", lambda: phase.freeze({"row": _Row(_MARK)})),
        ("dataclass-payload", lambda: _result(payload=[_Row(_PAN)])),
        ("dataclass-identity", lambda: _result(identity={"k": _Row(_MARK)})),  # type: ignore[dict-item]
        (
            "callee-generator",
            lambda: phase.FrozenMap((k, _raises_from_callee(v)) for k, v in {"k": _PAN}.items()),
        ),
        (
            "callee-contract-error",
            lambda: phase.FrozenMap(
                (k, _contract_error_from_callee(v)) for k, v in {"k": _MARK}.items()
            ),
        ),
        (
            "callee-unknown-reason",
            lambda: phase.to_wire(_result(reasons=(ReasonRef("post_scan", _MARK),))),
        ),
        (
            "callee-unknown-event-reason",
            lambda: phase.canonical_bytes(
                phase.PhaseEvent("post_scan", "e", "error", ReasonRef(_MARK, _PAN))
            ),
        ),
    ]


_IDS = [n for n, _ in _scenarios()]


def _raised(build: Callable[[], object]) -> phase.PhaseContractError:
    with pytest.raises(phase.PhaseContractError) as ei:
        build()
    return ei.value


def _hits(text: str) -> list[str]:
    return [m for m in _MARKERS if m in text]


def _chain(exc: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        stack += [cur.__context__, cur.__cause__]


def _surface_leaks(exc: BaseException) -> list[str]:
    out = _hits(str(exc)) + _hits(repr(exc)) + _hits(repr(exc.args))
    return out + _hits(repr(getattr(exc, "__notes__", None)))


def _engine_frame_locals(exc: BaseException) -> list[str]:
    te = traceback.TracebackException(type(exc), exc, exc.__traceback__, capture_locals=True)
    found: list[str] = []
    pending = [te]
    while pending:
        cur = pending.pop()
        found += [
            f"{fr.name}:{hit}"
            for fr in cur.stack
            if fr.filename == phase.__file__
            for hit in _hits(repr(fr.locals))
        ]
        pending += [t for t in (cur.__context__, cur.__cause__) if t is not None]
    return found


@pytest.mark.parametrize("name,build", _scenarios(), ids=_IDS)
def test_canary_error_text_args_and_notes_carry_no_marker(name, build):
    assert _surface_leaks(_raised(build)) == []


@pytest.mark.parametrize("name,build", _scenarios(), ids=_IDS)
def test_canary_error_has_no_context_or_cause(name, build):
    exc = _raised(build)
    # A fresh error raised outside the handler: nothing from the input is chained.
    assert exc.__context__ is None and exc.__cause__ is None
    assert [leak for e in _chain(exc) for leak in _surface_leaks(e)] == []


@pytest.mark.parametrize("name,build", _scenarios(), ids=_IDS)
def test_canary_traceback_locals_in_phase_frames_carry_no_marker(name, build):
    exc = _raised(build)
    assert _engine_frame_locals(exc) == []
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    assert _hits(rendered) == []


def test_canary_walk_is_not_vacuous():
    planted = ValueError(f"x {_MARK}")
    assert _surface_leaks(planted)
    try:
        try:
            raise planted
        except ValueError:
            raise phase.PhaseContractError("fixed") from None
    except phase.PhaseContractError as exc:
        assert [leak for e in _chain(exc) for leak in _surface_leaks(e)]
    for _, build in _scenarios():
        _raised(build)


def _callee_error_class() -> type[Exception]:
    # An ordinary callee-defined exception whose class name carries the marker.
    return type(f"Leak{_MARK}Error", (Exception,), {})


def _error_envelope() -> phase.PhaseResult:
    exc = _callee_error_class()(f"path /data/{_MARK}.csv value {_PAN}")
    exc.add_note(f"note {_MARK}")
    return phase.error_result(
        "post_scan",
        "integrity",
        "m",
        "1",
        reasons.get_reason("post_scan", "check_error").ref,
        exc,
    )


def _public_envelope_instances() -> dict[type, object]:
    res = _error_envelope()
    event = phase.PhaseEvent("post_scan", "check_failed", "error", res.reasons[0])
    return {
        phase.PhaseResult: res,
        phase.PhaseCoverage: res.coverage,
        phase.PhaseEvent: event,
        phase.FrozenMap: res.payload,
    }


def test_error_result_never_reads_the_exception_class_name_or_text():
    res = _error_envelope()
    wire = phase.canonical_bytes(res).decode()
    assert _hits(wire) == [] and "Leak" not in wire
    assert res.status == "error"


def test_error_result_records_a_fixed_kind_for_builtin_exceptions():
    ref = reasons.get_reason("post_scan", "check_error").ref
    res = phase.error_result("post_scan", "integrity", "m", "1", ref, OSError(_MARK))
    assert phase.to_wire(res)["payload"] == {"error_type": "OSError"}


def test_repr_and_wire_of_every_public_envelope_type_carry_no_marker():
    instances = _public_envelope_instances()
    public = {
        obj
        for name, obj in vars(phase).items()
        if not name.startswith("_")
        and isinstance(obj, type)
        and obj.__module__ == phase.__name__
        and (dataclasses.is_dataclass(obj) or obj is phase.FrozenMap)
    }
    assert public == set(instances)
    for obj in instances.values():
        assert _hits(repr(obj)) == []
    for env in (instances[phase.PhaseResult], instances[phase.PhaseEvent]):
        assert _hits(json.dumps(phase.to_wire(env))) == []  # type: ignore[arg-type]
        assert _hits(phase.canonical_bytes(env).decode()) == []  # type: ignore[arg-type]


class _PlainList(list):
    """An ordinary list subclass with no overridden behaviour."""


class _PlainTuple(tuple):
    """An ordinary tuple subclass with no overridden behaviour."""


class _PlainMap(phase.FrozenMap):
    """An ordinary FrozenMap subclass with no overridden behaviour."""


@pytest.mark.parametrize(
    "value",
    [
        _PlainList([1]),
        _PlainTuple((1,)),
        {"k": _PlainList([1])},
        [_PlainTuple(())],
        _PlainMap((("k", 1),)),
    ],
    ids=["list-sub", "tuple-sub", "nested-list-sub", "nested-tuple-sub", "frozenmap-sub"],
)
def test_freeze_refuses_container_subclasses_with_fixed_text(value):
    with pytest.raises(phase.PhaseContractError) as ei:
        phase.freeze(value)
    assert str(ei.value) == "cannot freeze this type into phase evidence"


def test_internal_rejection_text_survives_the_boundary():
    # Non-vacuity for the closed text mapping: a known rejection keeps its own fixed text.
    assert str(_raised(lambda: phase.PhaseCoverage(rows_examined=_PAN))) == (  # type: ignore[arg-type]
        "rows_examined must be int or None"
    )
    assert str(_raised(lambda: phase.to_wire(_result(status=_MARK)))) == "unknown status"
    assert str(_raised(lambda: phase.freeze(2**63))) == (
        "integer outside the 64-bit canonical range"
    )
    assert str(_raised(lambda: phase.FrozenMap(_raises_from_callee(v) for v in "a"))) == (
        "malformed phase evidence"
    )


def test_keyboard_interrupt_and_system_exit_pass_through_untouched():
    def gen(exc: BaseException):
        raise exc
        yield  # pragma: no cover

    for exc in (KeyboardInterrupt(), SystemExit(3)):
        with pytest.raises(type(exc)) as ei:
            phase.FrozenMap(gen(exc))
        assert ei.value is exc


_BOUNDARIES = [
    (phase.FrozenMap, "__new__"),
    (phase.PhaseCoverage, "__post_init__"),
    (phase.PhaseResult, "__post_init__"),
    (phase.PhaseEvent, "__post_init__"),
    (phase, "freeze"),
    (phase, "to_wire"),
    (phase, "canonical_bytes"),
]


def _canary_findings() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name, build in _scenarios():
        try:
            build()
        except phase.PhaseContractError as exc:
            leaks = [leak for e in _chain(exc) for leak in _surface_leaks(e)]
            leaks += _engine_frame_locals(exc)
            if exc.__context__ is not None or exc.__cause__ is not None:
                leaks.append("chained")
            found += [(name, leak) for leak in leaks]
        except Exception as exc:
            found.append((name, type(exc).__name__))
        else:
            found.append((name, "accepted"))
    return found


def test_canary_corpus_is_clean_with_every_boundary_in_place():
    assert _canary_findings() == []


@pytest.mark.parametrize(
    ("owner", "attr"), _BOUNDARIES, ids=[f"{getattr(o, '__name__', o)}.{a}" for o, a in _BOUNDARIES]
)
def test_canary_corpus_detects_each_boundary_removed(monkeypatch, owner, attr):
    raw = vars(owner)[attr]
    raw = raw.__func__ if isinstance(raw, staticmethod) else raw
    unwrapped = raw.__wrapped__
    monkeypatch.setattr(owner, attr, staticmethod(unwrapped) if attr == "__new__" else unwrapped)
    assert _canary_findings(), f"removing the boundary from {attr} went undetected"


# ---- the payload contract: accepted field content is caller-owned evidence ----------------------
# Characterization, not a leak test: the frozen `phase-result/v1` wire passes these through, and
# the envelope cannot tell a source value from an allowed one. Scrubbing, hashing or rejecting
# accepted content would break the wire; value-freeness of content is the producer's job (F31).

_PASS_THROUGH = [
    ("payload", lambda: _result(payload={"row": [_MARK], "n": _PAN})),
    ("identity", lambda: _result(identity={"source": _MARK})),
    ("check_id", lambda: _result(check_id=_MARK)),
    ("method", lambda: _result(method=_MARK)),
    ("method_version", lambda: _result(method_version=_MARK)),
    ("requested_scope", lambda: _result(coverage=phase.PhaseCoverage(requested_scope=(_MARK,)))),
    ("observed_scope", lambda: _result(coverage=phase.PhaseCoverage(observed_scope=(_MARK,)))),
    ("limits", lambda: _result(coverage=phase.PhaseCoverage(limits=(_MARK,)))),
    ("omissions", lambda: _result(coverage=phase.PhaseCoverage(omissions=(_MARK,)))),
    ("sampling_method", lambda: _result(coverage=phase.PhaseCoverage(sampling_method=_MARK))),
    ("event", lambda: phase.PhaseEvent("post_scan", _MARK, "passed")),
]


@pytest.mark.parametrize("name,build", _PASS_THROUGH, ids=[n for n, _ in _PASS_THROUGH])
def test_accepted_field_content_round_trips_unchanged(name, build):
    env = build()
    wire = json.dumps(phase.to_wire(env))
    assert _MARK in wire and _MARK in phase.canonical_bytes(env).decode()
    assert _MARK in repr(env)


def test_accepted_payload_is_exported_exactly_as_given():
    payload = {"row": [_MARK], "n": _PAN, "nested": {"k": [1, None, 2.5, True]}}
    res = _result(payload=payload)
    assert phase.to_wire(res)["payload"] == payload
    assert phase.satisfies_required_gate(res)
