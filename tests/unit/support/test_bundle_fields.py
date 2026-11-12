"""F32 T-E1: support bundle types carry no free text; labels, versions and reasons are closed."""

from __future__ import annotations

import copy
import dataclasses
import enum
import pickle
import platform
import sys
import types
import typing
from dataclasses import dataclass

import pytest

import decoy_engine
import decoy_engine.support as support
from decoy_engine.config._relationships import OrphanPolicyLiteral
from decoy_engine.config._tables import GenerateColumnConfig
from decoy_engine.determinism._derive import SEED_PROTOCOL_VERSION
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution.native._companion_status import Reason, native_companion_status
from decoy_engine.reasons import ReasonRef, route
from decoy_engine.reasons._record import COUNT_KEYS
from decoy_engine.reasons._registry import REGISTRY_VERSION
from decoy_engine.sdk import SDK_VERSION
from decoy_engine.support import (
    ColumnShape,
    DtypeClass,
    Label,
    Limits,
    NativeStatus,
    PlatformFamily,
    RatioBucket,
    StrategyKind,
    TableShape,
    Version,
    Versions,
)
from decoy_engine.support._shape import _LABEL_TOKEN, _Checked


def _mint(text: str) -> Label:
    return Label(text, _LABEL_TOKEN)


def _reachable(tp: object, seen: set[type]) -> set[type]:
    """Every bundle dataclass reachable from `tp`; `ReasonRef` is a registry-checked leaf."""
    if isinstance(tp, type) and dataclasses.is_dataclass(tp) and tp is not ReasonRef:
        if tp not in seen:
            seen.add(tp)
            for hint in typing.get_type_hints(tp).values():
                _reachable(hint, seen)
    for arg in typing.get_args(tp):
        _reachable(arg, seen)
    return seen


@dataclass(frozen=True)
class Leaky:
    name: str


@dataclass(frozen=True)
class Outer:
    inner: Leaky


@dataclass(frozen=True)
class Holder(_Checked):
    inner: Leaky


BUNDLE_TYPES = _reachable(support.SupportBundle, set())
_LEAVES = (bool, int, type(None), Label, Version, ReasonRef)


def _bad_hints(tp: object, path: str) -> list[str]:
    origin = typing.get_origin(tp)
    if origin is tuple:
        return [b for a in typing.get_args(tp) if a is not Ellipsis for b in _bad_hints(a, path)]
    if origin in (typing.Union, types.UnionType):
        return [b for a in typing.get_args(tp) for b in _bad_hints(a, path)]
    if origin is None and isinstance(tp, type):
        if tp in _LEAVES or issubclass(tp, enum.Enum) or dataclasses.is_dataclass(tp):
            return []
    return [f"{path}: {tp!r}"]


def _free_fields(cls: type) -> list[str]:
    hints = typing.get_type_hints(cls)
    return [b for f in dataclasses.fields(cls) for b in _bad_hints(hints[f.name], f.name)]


def test_no_free_str_field() -> None:
    assert _free_fields(Leaky) == ["name: <class 'str'>"]  # positive control
    assert _reachable(Outer, set()) == {Outer, Leaky}
    assert len(BUNDLE_TYPES) == 13
    for cls in BUNDLE_TYPES:
        assert cls.__dataclass_params__.frozen and issubclass(cls, _Checked), cls  # type: ignore[attr-defined]
        assert _free_fields(cls) == [], cls.__name__


def _column(**kw: object) -> ColumnShape:
    base: dict[str, object] = {
        "label": _mint("t1.c1"),
        "dtype": DtypeClass.INT,
        "strategy": StrategyKind.HASH,
        "nullable": RatioBucket.ZERO,
        "distinct": RatioBucket.UNKNOWN,
        "length_log2": None,
        "pk": True,
        "fk": False,
        "pii": None,
    }
    base.update(kw)
    return ColumnShape(**base)  # type: ignore[arg-type]


def test_label_rejects_name() -> None:
    for text in ("MARKER_T_ab12", "t1.c1; x", "t1"):
        with pytest.raises((TypeError, ValueError)):
            Label(text)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Label("MARKER_T_ab12", _LABEL_TOKEN)
    good = _mint("t1")
    assert good == "t1" and type(good) is Label
    forged = str.__new__(Label, "MARKER_T_ab12")
    for bad in ("t1", forged, str.__new__(Label, "t1.c1")):
        with pytest.raises((TypeError, ValueError)):
            TableShape(label=bad, rows_log2=None, columns=())  # type: ignore[arg-type]


@dataclass(frozen=True)
class ReasonHolder(_Checked):
    reason: ReasonRef


@dataclass(frozen=True)
class NameHolder(_Checked):
    name: str


class _StrSub(str):
    pass


def test_reason_ref_rejects_unregistered() -> None:
    for ns, code in (("support", "no_such_code"), ("no_such_ns", "fk_relationship")):
        with pytest.raises(ValueError, match="not a registered reason"):
            ReasonHolder(ReasonRef(ns, code))


def test_reason_ref_fields_must_be_exact_str() -> None:
    for ns, code in (
        (_StrSub("route"), "fk_relationship"),
        ("route", _StrSub("fk_relationship")),
        ("route", route.FK_RELATIONSHIP),
        ("route", 7),
        (None, "fk_relationship"),
    ):
        with pytest.raises(ValueError, match="not a registered reason"):
            ReasonHolder(ReasonRef(ns, code))  # type: ignore[arg-type]


def test_reason_ref_registered_is_accepted() -> None:
    # `route.FK_RELATIONSHIP` is a `ReasonCode` (a str subclass); `spec.ref` carries plain str.
    for ref in (ReasonRef("route", "fk_relationship"), route.FK_RELATIONSHIP.spec.ref):
        assert ReasonHolder(ref).reason == ReasonRef("route", "fk_relationship")
    holder = ReasonHolder(ReasonRef("route", "fk_relationship"))
    assert type(holder.reason) is ReasonRef


def test_reason_ref_error_names_no_value() -> None:
    with pytest.raises(ValueError) as err:
        ReasonHolder(ReasonRef("MARKER_NS", "MARKER_CODE"))
    assert "MARKER" not in str(err.value)
    assert str(err.value) == "ReasonHolder.reason is not a registered reason"


def test_str_field_is_not_a_leaf() -> None:
    with pytest.raises(TypeError):
        NameHolder(name="x")


def _versions(**kw: object) -> Versions:
    base: dict[str, object] = {
        "engine": Version(decoy_engine.__version__),
        "sdk": Version(SDK_VERSION),
        "registry": Version(REGISTRY_VERSION),
        "product": Version("0.18.0"),
        "python": Version(platform.python_version()),
        "seed_protocol": SEED_PROTOCOL_VERSION,
        "platform": PlatformFamily.of(sys.platform),
        "native": NativeStatus(native_companion_status().reason),
    }
    base.update(kw)
    return Versions(**base)  # type: ignore[arg-type]


def test_versions_from_live_constants() -> None:
    v = _versions()
    assert v.sdk == SDK_VERSION == "1.0"
    assert v.engine == decoy_engine.__version__ and v.registry == REGISTRY_VERSION
    assert v.seed_protocol == SEED_PROTOCOL_VERSION
    assert PlatformFamily.of("linux") is PlatformFamily.LINUX
    assert PlatformFamily.of("freebsd14") is PlatformFamily.OTHER


def test_version_rejects_free_text() -> None:
    for text in (
        "1.0; rm",
        "1",
        "1.0\n",
        chr(0x661) + "." + chr(0x660),
        "1.0.0.0.0",
        "1.0.0-",
        "",
        "1" * 7 + ".0",
        "1.0-MARKERSECRET",
        "1.0.0-abc123",
        "1.0.0.x1",
        "1.0.0rc",
        "1.0.0rc1.dev2",
    ):
        with pytest.raises(ValueError):
            Version(text)
    for text in ("1.0", "0.23.0", "1.0.0rc1", "0.23.0.dev3", "1.2-b2", "3.13.0a1", "1.0.post1"):
        assert Version(text) == text
    for kw in (
        {"seed_protocol": "7"},
        {"seed_protocol": True},
        {"seed_protocol": 2**31},
        {"seed_protocol": -1},
        {"sdk": "1.0"},
        {"native": "absent"},
    ):
        with pytest.raises((TypeError, ValueError)):
            _versions(**kw)


def test_label_survives_pickle_copy_asdict() -> None:
    label = _mint("t3.c2")
    assert pickle.loads(pickle.dumps(label)) == label  # noqa: S301
    assert type(pickle.loads(pickle.dumps(label))) is Label  # noqa: S301
    assert copy.copy(label) is label and copy.deepcopy(label) is label
    column = _column()
    assert type(dataclasses.asdict(column)["label"]) is Label
    assert pickle.loads(pickle.dumps(column)) == column  # noqa: S301


def test_nested_plain_dataclass_is_refused() -> None:
    with pytest.raises(TypeError):
        Holder(inner=Leaky("x"))


def test_types_reject_lookalikes() -> None:
    class Sneaky(str):
        pass

    for kw in (
        {"label": Sneaky("t1.c1")},
        {"pk": 1},
        {"length_log2": True},
        {"length_log2": 64},
        {"dtype": "int"},
    ):
        with pytest.raises((TypeError, ValueError)):
            _column(**kw)
    assert _column().pk is True
    with pytest.raises((TypeError, ValueError)):
        Limits(threads=None, peak_memory_mib=2**31)


def test_mirrored_enums_match_engine() -> None:
    assert {m.value for m in support.CountKey} == set(COUNT_KEYS)
    assert {m.value for m in NativeStatus} == set(typing.get_args(Reason))
    assert {m.value for m in support.OrphanPolicy} == set(typing.get_args(OrphanPolicyLiteral))
    gen = typing.get_args(GenerateColumnConfig.model_fields["type"].annotation)
    accepted = set(SCALAR_HANDLERS) | set(gen) | {"from_parent"}
    assert {m.value for m in StrategyKind} == accepted | {"other"}
    assert StrategyKind.PASSTHROUGH.value == "passthrough"
