"""F32 T-E1: support bundle types carry no free text; labels, versions and reasons are closed."""

from __future__ import annotations

import dataclasses
import enum
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
from decoy_engine.reasons import route
from decoy_engine.reasons._record import COUNT_KEYS
from decoy_engine.reasons._registry import REGISTRY_VERSION
from decoy_engine.sdk import SDK_VERSION
from decoy_engine.support import (
    ColumnShape,
    DtypeClass,
    Label,
    LabelAllocator,
    Limits,
    NativeStatus,
    PlatformFamily,
    RatioBucket,
    ReasonRef,
    StrategyKind,
    TableShape,
    Version,
    Versions,
)

BUNDLE_TYPES = (
    support.SupportBundle,
    support.Versions,
    support.Limits,
    support.RouteFact,
    support.Timings,
    support.PhaseTiming,
    support.NodeTiming,
    support.ErrorFact,
    support.Topology,
    support.TableShape,
    support.ColumnShape,
    support.EdgeShape,
    support.EventFact,
)
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
    @dataclass(frozen=True)
    class Leaky:
        name: str

    assert _free_fields(Leaky) == ["name: <class 'str'>"]  # positive control
    for cls in BUNDLE_TYPES:
        assert dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen, cls
        assert _free_fields(cls) == [], cls.__name__


def _column(alloc: LabelAllocator, **kw: object) -> ColumnShape:
    base: dict[str, object] = {
        "label": alloc.column("MARKER_T_ab12", "MARKER_C_cd34"),
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
    alloc = LabelAllocator()
    good = alloc.table("MARKER_T_ab12")
    assert good == "t1" and type(good) is Label
    forged = str.__new__(Label, "MARKER_T_ab12")
    for bad in ("t1", forged, str.__new__(Label, "t1.c1")):
        with pytest.raises((TypeError, ValueError)):
            TableShape(label=bad, rows_log2=None, columns=())  # type: ignore[arg-type]


def test_reason_ref_rejects_unregistered() -> None:
    for ns, code in (("support", "no_such_code"), ("no_such_ns", "fk_relationship"), ("route", 7)):
        with pytest.raises(ValueError):
            ReasonRef(ns, code)
    ref = ReasonRef("route", route.FK_RELATIONSHIP)
    assert (ref.namespace, ref.code) == ("route", "fk_relationship")
    assert ref == ReasonRef("route", "fk_relationship")
    with pytest.raises(AttributeError):
        ref.code = "x"  # type: ignore[misc]


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
    for text in ("1.0; rm", "1", "1.0\n", chr(0x661) + "." + chr(0x660), "1.0.0.0.0", "1.0.0-", ""):
        with pytest.raises(ValueError):
            Version(text)
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


def test_types_reject_lookalikes() -> None:
    alloc = LabelAllocator()

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
            _column(alloc, **kw)
    assert _column(alloc).pk is True
    with pytest.raises((TypeError, ValueError)):
        Limits(threads=None, peak_memory_mib=2**31)
    many = LabelAllocator()
    for i in range(9999):
        many.table(i)
    with pytest.raises(ValueError):
        many.table("one more")


def test_mirrored_enums_match_engine() -> None:
    assert {m.value for m in support.CountKey} == set(COUNT_KEYS)
    assert {m.value for m in NativeStatus} == set(typing.get_args(Reason))
    assert {m.value for m in support.OrphanPolicy} == set(typing.get_args(OrphanPolicyLiteral))
    gen = typing.get_args(GenerateColumnConfig.model_fields["type"].annotation)
    accepted = set(SCALAR_HANDLERS) | set(gen) | {"from_parent"}
    assert {m.value for m in StrategyKind} == accepted | {"other"}
    assert StrategyKind.PASSTHROUGH.value == "passthrough"
