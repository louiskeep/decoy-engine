"""Every public callable in `phase.__all__` is routed through the one boundary, `phase._contained`.

The boundary marks what it wraps with `__phase_contained__`; this sentry enumerates the export
list at test time. A function must carry the mark itself; a class must define `__new__` or
`__post_init__` carrying it. Exception classes are raised, not called by callers, so they are
not entry points. Hostile aliasing and descriptor games are V2 scope, not policed here.
"""

from __future__ import annotations

import inspect
from types import ModuleType, SimpleNamespace

from decoy_engine import phase


def _marked(fn: object) -> bool:
    return getattr(fn, "__phase_contained__", False) is True


def _uncontained(module: ModuleType | SimpleNamespace) -> list[str]:
    bad: list[str] = []
    for name in module.__all__:
        obj = getattr(module, name)
        if inspect.isclass(obj):
            if issubclass(obj, BaseException):
                continue
            entries = [a for a in ("__new__", "__post_init__") if a in vars(obj)]
            if not entries:
                bad.append(f"{name} has no contained __new__/__post_init__")
            bad += [f"{name}.{a}" for a in entries if not _marked(getattr(obj, a))]
        elif inspect.isfunction(obj) and not _marked(obj):
            bad.append(name)
    return bad


def test_every_exported_phase_callable_is_contained():
    assert _uncontained(phase) == []


def test_every_public_phase_callable_is_exported():
    # Without this, a new public function could skip the sentry by staying out of `__all__`.
    defined = {
        name
        for name, obj in vars(phase).items()
        if not name.startswith("_")
        and (inspect.isfunction(obj) or inspect.isclass(obj))
        and obj.__module__ == phase.__name__
    }
    assert defined <= set(phase.__all__)


def test_sentry_flags_an_uncontained_function_and_constructor():
    def loose():
        return None

    class Envelope:
        def __post_init__(self):
            return None

    class Bare:
        pass

    fake = SimpleNamespace(
        __all__=["loose", "Envelope", "Bare", "ok", "Err"],
        loose=loose,
        Envelope=Envelope,
        Bare=Bare,
        ok=phase._contained(loose),
        Err=ValueError,
    )
    assert _uncontained(fake) == [
        "loose",
        "Envelope.__post_init__",
        "Bare has no contained __new__/__post_init__",
    ]
