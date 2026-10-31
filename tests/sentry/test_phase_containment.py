"""Every name in `phase.__all__` is classified: a checked entry point or a listed exemption.

The boundary marks what it wraps with `__phase_contained__`; this sentry enumerates the export
list at test time. Checked: a function must carry the mark itself; a non-exception class must
define `__new__` or `__post_init__` carrying it. Exempt: names listed in `EXEMPT` with a reason.
Functions and non-exception classes are always checked, so the exempt list cannot hide an entry
point, and a name in neither category fails. Hostile aliasing and descriptor games are V2 scope.
"""

from __future__ import annotations

import inspect
from types import ModuleType, SimpleNamespace

from decoy_engine import phase

EXEMPT: dict[str, str] = {
    "PhaseContractError": "exception type: raised by the boundary, not an entry point",
    "Phase": "typing.Literal alias: runtime-callable, but calling it only raises TypeError",
    "Status": "typing.Literal alias: runtime-callable, but calling it only raises TypeError",
    "FrozenScalar": "type alias (union), never called",
    "FrozenValue": "type alias (union), never called",
    "RESULT_SCHEMA": "str constant",
    "EVENT_SCHEMA": "str constant",
    "INT_MIN": "int bound",
    "INT_MAX": "int bound",
    "MAX_DEPTH": "int bound",
    "PHASES": "frozenset constant",
    "STATUSES": "frozenset constant",
}


def _marked(fn: object) -> bool:
    return getattr(fn, "__phase_contained__", False) is True


def _is_checked(obj: object) -> bool:
    if inspect.isclass(obj):
        return not issubclass(obj, BaseException)
    return inspect.isfunction(obj)


def _uncontained(
    module: ModuleType | SimpleNamespace, exempt: dict[str, str] = EXEMPT
) -> list[str]:
    bad: list[str] = []
    for name in module.__all__:
        obj = getattr(module, name)
        if not _is_checked(obj):
            if name not in exempt:
                bad.append(f"{name} is neither checked nor exempt")
            continue
        if inspect.isclass(obj):
            entries = [a for a in ("__new__", "__post_init__") if a in vars(obj)]
            if not entries:
                bad.append(f"{name} has no contained __new__/__post_init__")
            bad += [f"{name}.{a}" for a in entries if not _marked(getattr(obj, a))]
        elif not _marked(obj):
            bad.append(name)
    return bad


def test_every_exported_phase_callable_is_contained():
    assert _uncontained(phase) == []


def test_exempt_list_names_only_real_exports():
    assert set(EXEMPT) <= set(phase.__all__)


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
    assert _uncontained(fake, {"Err": "exception type"}) == [
        "loose",
        "Envelope.__post_init__",
        "Bare has no contained __new__/__post_init__",
    ]


def test_sentry_flags_an_unclassified_export():
    import typing

    fake = SimpleNamespace(__all__=["Mystery"], Mystery=typing.Literal["a"])
    assert _uncontained(fake) == ["Mystery is neither checked nor exempt"]


def test_exempt_listing_cannot_hide_an_entry_point():
    def loose():
        return None

    fake = SimpleNamespace(__all__=["loose"], loose=loose)
    assert _uncontained(fake, {"loose": "claimed exempt"}) == ["loose"]
