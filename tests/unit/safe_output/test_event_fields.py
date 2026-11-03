"""F31 E0a: `SafeEvent` takes only exact, closed, bounded fields (the field walk)."""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Mapping
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.reasons import ReasonRef, Severity
from decoy_engine.safe_output import EventKind, Flag, SafeErrorType, SafeEvent, StepRef

_REF = reasons.get_reason("post_scan", "check_error").ref
_MAX_SEQ = 2**53 - 1
_MAX_TS = 253_402_300_799_999
_BOUNDARY_KEYS = {"retry_class", "publication_state", "cleanup_state", "storage_ref"}


class _Int(int):
    pass


class _Str(str):
    pass


class _Ref(ReasonRef):
    pass


class _Step(StepRef):
    pass


def _full(**over: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "seq": 7,
        "ts_ms": 1_700_000_000_000,
        "sev": Severity.ERROR,
        "kind": EventKind.STEP,
        "reason": _REF,
        "err": SafeErrorType.from_label("KeyError"),
        "step": StepRef("orders"),
        "counts": (("errors", 1), ("rows", 5)),
        "flags": frozenset({Flag.TRUNCATED}),
    }
    fields.update(over)
    return fields


_HOSTILE: list[tuple[str, str, Any]] = [
    ("seq", "str", "1"),
    ("seq", "bool", True),
    ("seq", "int_subclass", _Int(1)),
    ("seq", "float", 1.0),
    ("seq", "none", None),
    ("seq", "negative", -1),
    ("seq", "bound_plus_one", _MAX_SEQ + 1),
    ("ts_ms", "str", "1"),
    ("ts_ms", "bool", False),
    ("ts_ms", "int_subclass", _Int(1)),
    ("ts_ms", "none", None),
    ("ts_ms", "negative", -1),
    ("ts_ms", "bound_plus_one", _MAX_TS + 1),
    ("sev", "plain_str", "error"),
    ("sev", "other_enum", EventKind.STEP),
    ("sev", "none", None),
    ("kind", "plain_str", "step"),
    ("kind", "other_enum", Severity.ERROR),
    ("kind", "none", None),
    ("reason", "none", None),
    ("reason", "tuple", ("post_scan", "check_error")),
    ("reason", "subclass", _Ref("post_scan", "check_error")),
    ("reason", "str_subclass_parts", ReasonRef(_Str("post_scan"), _Str("check_error"))),
    ("reason", "unregistered", ReasonRef("nope", "nope")),
    ("err", "plain_str", "KeyError"),
    ("err", "other_enum", Severity.ERROR),
    ("step", "plain_str", "orders"),
    ("step", "subclass", _Step("orders")),
    ("counts", "dict", {"errors": 1}),
    ("counts", "list", [("errors", 1)]),
    ("counts", "unsorted", (("rows", 1), ("errors", 1))),
    ("counts", "duplicate", (("errors", 1), ("errors", 2))),
    ("counts", "unknown_key", (("secrets", 1),)),
    ("counts", "str_subclass_key", ((_Str("errors"), 1),)),
    ("counts", "negative", (("errors", -1),)),
    ("counts", "bool_value", (("errors", True),)),
    ("counts", "list_pair", (["errors", 1],)),
    ("counts", "triple", (("errors", 1, 2),)),
    ("counts", "seventeen", tuple(("errors", 1) for _ in range(17))),
    ("flags", "set", {Flag.TRUNCATED}),
    ("flags", "str_member", frozenset({"truncated"})),
    ("flags", "none", None),
]


@pytest.mark.parametrize(
    ("field", "value"),
    [(f, v) for f, _, v in _HOSTILE],
    ids=[f"{f}-{h}" for f, h, _ in _HOSTILE],
)
def test_field_walk(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        SafeEvent(**_full(**{field: value}))


@pytest.mark.parametrize(
    "name",
    ["a/b", "x://y", "", "a" * 129, "tab\there", 3, None, _Str("orders")],
    ids=["slash", "url", "empty", "too_long", "control", "int", "none", "str_subclass"],
)
def test_step_ref_refuses(name: Any) -> None:
    with pytest.raises(ValueError):
        StepRef(name)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("counts", {"errors": 1}),
        ("seq", True),
        ("reason", ReasonRef("nope", "nope")),
        ("sev", "error"),
        ("flags", frozenset({"truncated"})),
    ],
    ids=["counts", "seq", "reason", "sev", "flags"],
)
def test_forged_instance_caught_by_to_dict(field: str, value: Any) -> None:
    event = SafeEvent(**_full())
    object.__setattr__(event, field, value)
    with pytest.raises(ValueError):
        event.to_dict()


def test_forged_step_name_caught_by_to_dict() -> None:
    event = SafeEvent(**_full())
    object.__setattr__(event.step, "name", "x://y")
    with pytest.raises(ValueError):
        event.to_dict()


def _free_text_fields(cls: type) -> list[str]:
    hints = typing.get_type_hints(cls)
    found = []
    for f in dataclasses.fields(cls):
        hint = hints[f.name]
        parts = typing.get_args(hint) if typing.get_origin(hint) is not None else (hint,)
        if typing.get_origin(hint) in (dict, Mapping) or any(
            p is str or p is Any or p is dict or p is Mapping for p in parts
        ):
            found.append(f.name)
    return found


def test_no_free_text_field() -> None:
    assert _free_text_fields(SafeEvent) == []
    assert _free_text_fields(StepRef) == ["name"]  # approved by `approve_name` at construction

    @dataclasses.dataclass(frozen=True)
    class _Seeded(SafeEvent):
        msg: str = ""

    assert _free_text_fields(_Seeded) == ["msg"]


def _depth(value: object) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    return set()


def test_valid_event_round_trips_to_dict() -> None:
    full = SafeEvent(**_full(seq=_MAX_SEQ, ts_ms=_MAX_TS))
    assert full.to_dict() == {
        "s": "decoy-safe-output/v1",
        "seq": _MAX_SEQ,
        "ts": _MAX_TS,
        "sev": "error",
        "kind": "step",
        "reason": {"ns": "post_scan", "code": "check_error"},
        "err": "KeyError",
        "step": "orders",
        "counts": {"errors": 1, "rows": 5},
        "flags": ["truncated"],
    }
    minimal = SafeEvent(seq=0, ts_ms=0, sev=Severity.ERROR, kind=EventKind.INVALID, reason=_REF)
    assert minimal.to_dict() == {
        "s": "decoy-safe-output/v1",
        "seq": 0,
        "ts": 0,
        "sev": "error",
        "kind": "invalid",
        "reason": {"ns": "post_scan", "code": "check_error"},
    }
    for event in (full, minimal):
        wire = event.to_dict()
        assert _depth(wire) <= 2
        assert not _keys(wire) & _BOUNDARY_KEYS
        assert all(type(v) in (str, int, dict, list) for v in wire.values())


def test_closed_enums() -> None:
    assert [m.value for m in EventKind] == ["narrative", "step", "terminal", "withheld", "invalid"]
    assert [m.value for m in Flag] == ["truncated"]
