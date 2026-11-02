"""A nested engine call never widens the installed policy, even when it names one explicitly.

"Not broader" is the only ordering the policies have: the ambient CLI policy is the widest, and
two strict policies are incomparable unless they are the same object. So under an installed
policy an explicit one is accepted when it is that same object or when the installed one is
ambient; any other explicit policy is refused with `FileAccessUnavailable`, raised bare.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.fileaccess import (
    FileAccessUnavailable,
    LocalFileAccess,
    _entry,
    capture,
    current,
    entry_point,
    require_ambient,
    scope,
)
from tests.fileaccess.support import RootRelative


@pytest.fixture
def two_strict(tmp_path: Path) -> Iterator[tuple[RootRelative, RootRelative]]:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, b = RootRelative(tmp_path / "a"), RootRelative(tmp_path / "b")
    try:
        yield a, b
    finally:
        a.close()
        b.close()


@entry_point
def _leaf(*, file_access: Any = None) -> object:
    return current()


def _via_entry(explicit: object) -> object:
    return _leaf(file_access=explicit)


def _via_scope(explicit: object) -> object:
    with scope(explicit) as policy:  # type: ignore[arg-type]
        return policy


def _via_capture(explicit: object) -> object:
    return capture(explicit)  # type: ignore[arg-type]


ROUTES: dict[str, Callable[[object], object]] = {
    "entry_point": _via_entry,
    "scope": _via_scope,
    "capture": _via_capture,
}


def _pair(name: str, a: RootRelative, b: RootRelative) -> tuple[object, object]:
    """(installed, explicit) for each ordering pair."""
    ambient, ambient2 = LocalFileAccess(), LocalFileAccess()
    return {
        "strict_installed_ambient_explicit": (a, ambient),
        "strict_installed_other_strict_explicit": (a, b),
        "strict_installed_same_object": (a, a),
        "ambient_installed_strict_explicit": (ambient, a),
        "ambient_installed_same_object": (ambient, ambient),
        "ambient_installed_other_ambient": (ambient, ambient2),
    }[name]


REFUSED = ("strict_installed_ambient_explicit", "strict_installed_other_strict_explicit")
ACCEPTED = (
    "strict_installed_same_object",
    "ambient_installed_strict_explicit",
    "ambient_installed_same_object",
    "ambient_installed_other_ambient",
)


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("pair", REFUSED)
def test_an_explicit_policy_broader_than_or_unrelated_to_the_installed_one_is_refused(
    two_strict: tuple[RootRelative, RootRelative], pair: str, route: str
) -> None:
    installed, explicit = _pair(pair, *two_strict)
    with scope(installed):  # type: ignore[arg-type]
        carrier = b"CALLER-CANARY\xff"
        try:
            carrier.decode("utf-8")
        except UnicodeDecodeError:
            with pytest.raises(FileAccessUnavailable) as info:
                ROUTES[route](explicit)
        assert current() is installed
    exc = info.value
    assert exc.__cause__ is None and exc.__context__ is None and exc.__suppress_context__


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("pair", ACCEPTED)
def test_an_explicit_policy_no_broader_than_the_installed_one_is_used(
    two_strict: tuple[RootRelative, RootRelative], pair: str, route: str
) -> None:
    installed, explicit = _pair(pair, *two_strict)
    with scope(installed):  # type: ignore[arg-type]
        assert ROUTES[route](explicit) is explicit


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_an_omitted_policy_inherits_the_installed_strict_one(
    two_strict: tuple[RootRelative, RootRelative], route: str
) -> None:
    a, _ = two_strict
    with scope(a):
        assert ROUTES[route](None) is a


def test_a_nested_entry_inside_a_strict_call_cannot_switch_to_ambient(
    two_strict: tuple[RootRelative, RootRelative],
) -> None:
    a, _ = two_strict

    @entry_point
    def outer(*, file_access: Any = None) -> object:
        return _leaf(file_access=LocalFileAccess())

    with pytest.raises(FileAccessUnavailable):
        outer(file_access=a)


UNRESOLVED: dict[str, Callable[[], object]] = {
    "entry_point": lambda: _leaf(),
    "scope": lambda: _via_scope(None),
    "capture": lambda: capture(),
    "current": lambda: current(),
    "require_ambient": lambda: require_ambient("a scratch route"),
    "forbidden_explicit_ambient": lambda: _leaf(file_access=LocalFileAccess()),
}


@pytest.mark.parametrize("route", sorted(UNRESOLVED))
def test_every_resolution_refusal_is_raised_bare_inside_a_callers_handler(
    route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_entry, "_ambient_forbidden", True)
    token = _entry._CURRENT.set(None)
    try:
        try:
            b"CALLER-CANARY\xff".decode("utf-8")
        except UnicodeDecodeError:
            with pytest.raises(FileAccessUnavailable) as info:
                UNRESOLVED[route]()
    finally:
        _entry._CURRENT.reset(token)
    exc = info.value
    assert exc.__cause__ is None and exc.__context__ is None and exc.__suppress_context__
