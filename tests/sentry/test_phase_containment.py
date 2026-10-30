"""Every public phase-envelope constructor and serializer goes through `phase._contained`.

The containment wrapper is the single place a foreign escape becomes a fixed-text
`PhaseContractError`; a public entry point that bypasses it is one hostile object away from
leaking a raw exception (and its text) to a caller.
"""

from __future__ import annotations

import inspect
from types import ModuleType, SimpleNamespace

from decoy_engine import phase

_CONTAINED_CODE = phase._contained(lambda: None).__code__


def _is_contained(fn) -> bool:
    fn = fn.__func__ if isinstance(fn, (staticmethod, classmethod)) else fn
    return getattr(fn, "__code__", None) is _CONTAINED_CODE


def _uncontained(module: ModuleType | SimpleNamespace, modname: str) -> list[str]:
    bad: list[str] = []
    for name, obj in vars(module).items():
        if name.startswith("_") or getattr(obj, "__module__", None) != modname:
            continue
        if inspect.isclass(obj):
            if issubclass(obj, BaseException):
                continue
            entries = [a for a in ("__new__", "__post_init__") if a in obj.__dict__]
            if not entries:
                bad.append(f"{name} has no contained __new__/__post_init__")
            bad += [f"{name}.{a}" for a in entries if not _is_contained(obj.__dict__[a])]
        elif inspect.isfunction(obj) and not _is_contained(obj):
            bad.append(name)
    return bad


def test_every_public_phase_entry_point_is_contained():
    assert _uncontained(phase, phase.__name__) == []


def test_sentry_flags_an_uncontained_function_and_constructor():
    def loose():
        return None

    class Envelope:
        def __post_init__(self):
            return None

    for obj in (loose, Envelope):
        obj.__module__ = "fake"
    fake = SimpleNamespace(loose=loose, Envelope=Envelope, ok=phase._contained(loose))
    fake.ok.__module__ = "fake"
    assert _uncontained(fake, "fake") == ["loose", "Envelope.__post_init__"]
