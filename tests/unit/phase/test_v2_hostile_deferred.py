"""Hostile in-process objects: recorded for V2 hostile hardening, not a V1 gate.

The V1 threat model (owner decision 2026-10-30) trusts the engine's own process: callers pass
ordinary values. These cases (overridden dunders, forged instances, `BaseException` from a
dunder, `__del__`) stay here as the V2 backlog. They run but never fail the suite.
"""

from __future__ import annotations

import sys

import pytest
from test_phase_contract import _R7, _r7_escapes, _r7_scalars

from decoy_engine import phase

pytestmark = pytest.mark.xfail(strict=False, reason="deferred to V2 hostile hardening")


def _boom(*_a, **_k):
    raise RuntimeError(_R7)


_HOSTILE_DUNDERS = (
    "__repr__",
    "__len__",
    "__iter__",
    "__eq__",
    "__hash__",
    "__lt__",
    "__getattr__",
)


def _hostile_type(base: type, dunder: str) -> type:
    ns: dict = {dunder: _boom}
    if dunder == "__eq__":
        ns["__hash__"] = base.__hash__ if base is not object else object.__hash__
    return type(f"Hostile{base.__name__.title()}{dunder.strip('_').title()}", (base,), ns)


class _ForeignContractError(list):
    """Foreign code raising the engine's own error type with the input in the text."""

    def __iter__(self):
        raise phase.PhaseContractError(_R7)

    def __len__(self):
        raise phase.PhaseContractError(_R7)


class _RaisingGetattribute:
    def __getattribute__(self, name):
        raise RuntimeError(_R7)


def _hostile_scalars() -> list:
    out: list = []
    for dunder in _HOSTILE_DUNDERS:
        out.append(_hostile_type(object, dunder)())
        out.append(_hostile_type(int, dunder)(3))
        out.append(_hostile_type(str, dunder)("ok"))
        out.append(_hostile_type(tuple, dunder)(("k", 1)))
        out.append(_hostile_type(float, dunder)(1.5))
        out.append(_hostile_type(list, dunder)(["k"]))
        out.append(_hostile_type(dict, dunder)({"k": 1}))
    return [*out, _RaisingGetattribute(), _ForeignContractError()]


def test_field_walk_with_hostile_dunder_corpus():
    escapes, _ = _r7_escapes(_r7_scalars() + _hostile_scalars())
    assert sorted(set(escapes)) == []


def test_frozenmap_rejects_subclassed_pairs_and_primitives_with_fixed_text():
    for bad in (
        (_hostile_type(tuple, "__len__")(("k", 1)),),
        (("k", _hostile_type(int, "__lt__")(1)),),
        (("k", _hostile_type(str, "__eq__")("v")),),
        ((_hostile_type(str, "__lt__")("k"), 1), ("j", 2)),
        (("k", _hostile_type(tuple, "__iter__")((1,))),),
    ):
        with pytest.raises(phase.PhaseContractError) as ei:
            phase.FrozenMap(bad)
        assert _R7 not in str(ei.value)


class _HostileInterrupt(BaseException):
    pass


class _BaseExceptionIter(list):
    def __iter__(self):
        raise _HostileInterrupt(_R7)


def test_base_exception_from_a_dunder_is_contained():
    # V1 deliberately lets BaseException through so KeyboardInterrupt/SystemExit stay intact.
    with pytest.raises(phase.PhaseContractError):
        phase.FrozenMap(_BaseExceptionIter())


class _ClassSpoof:
    @property  # type: ignore[misc]
    def __class__(self):
        raise RuntimeError(_R7)


def test_class_property_spoof_is_contained():
    for build in (lambda: phase.freeze(_ClassSpoof()), lambda: phase.FrozenMap(_ClassSpoof())):
        with pytest.raises(phase.PhaseContractError) as ei:
            build()
        assert _R7 not in str(ei.value)


def test_forged_frozenmap_via_tuple_new_is_refused():
    forged = tuple.__new__(phase.FrozenMap, [("k", object()), ("k", 1)])
    with pytest.raises(phase.PhaseContractError):
        phase.freeze(forged)


class _LeakyDel:
    def __del__(self):
        raise RuntimeError(_R7)


def test_del_raising_during_rejection_does_not_reach_unraisablehook(monkeypatch):
    seen: list = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: seen.append(str(u.exc_value)))
    with pytest.raises(phase.PhaseContractError):
        phase.freeze([_LeakyDel()])
    assert not any(_R7 in s for s in seen)


def test_catch_all_detects_containment_removed_from_coverage(monkeypatch):
    unwrapped = phase.PhaseCoverage.__dict__["__post_init__"].__wrapped__
    monkeypatch.setattr(phase.PhaseCoverage, "__post_init__", unwrapped)
    escapes, _ = _r7_escapes(_r7_scalars() + _hostile_scalars())
    assert escapes
