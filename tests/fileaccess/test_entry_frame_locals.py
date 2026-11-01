"""No engine frame on a failing admission or entry-point call holds the ref, path or secret.

`traceback.TracebackException(capture_locals=True)` prints the repr of every local of every frame
on the traceback. The class is every public admission entry (they take a ref, a config holding
one, or a provider holding material) plus the `entry_point` decorator, whose wrapper frame would
otherwise keep each caller argument in `args`/`kwargs`. Each is a cell, with the canary as a bare
string, nested in a tuple and nested in a dict.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.fileaccess import (
    FileAccessUnavailable,
    _entry,
    entry_point,
    run_in_context,
    scope,
)
from decoy_engine.keyprovider import MaskSecretError, key_provider_from_ref
from decoy_engine.secret_providers import (
    REQUIREMENTS,
    admit_mask_key,
    parse_ref,
    prepare_legacy,
    prepare_ref,
    prepare_secret,
)
from decoy_engine.secret_providers._local import FileSecretProvider
from tests.fileaccess.locals_support import assert_no_engine_local_holds
from tests.fileaccess.support import RootRelative

CANARY = "tenant-a/private/LOCALS-" + uuid.uuid4().hex[:8]
# Shorter than the 32-byte floor, so a provider returning it is refused.
SHORT_KEY = "KEY-" + uuid.uuid4().hex[:8]
MASK_ROOT = (REQUIREMENTS["decoy-mask-root-v1"],)


@pytest.fixture
def policy(tmp_path: Path) -> Iterator[RootRelative]:
    (tmp_path / "root").mkdir()
    p = RootRelative(tmp_path / "root")
    try:
        yield p
    finally:
        p.close()


class _CanaryKey:
    """A duck-typed `KeyProvider` whose short key is a canary: admission refuses it."""

    key_version = "v1"

    def mask_key(self) -> bytes:
        return SHORT_KEY.encode()


def _ref() -> str:
    return f"file:/{CANARY}/secret.key"


ADMISSION: dict[str, Callable[[Any], Any]] = {
    "key_provider_from_ref": lambda p: key_provider_from_ref(_ref(), file_access=p),
    "key_provider_from_ref_kw": lambda p: key_provider_from_ref(
        ref=_ref(), key_version=CANARY, file_access=p
    ),
    "prepare_ref": lambda p: prepare_ref(_ref(), key_version=CANARY),
    "parse_ref": lambda p: parse_ref(f"file:/{CANARY}?encoding=nope"),
    "admit_mask_key": lambda p: admit_mask_key(
        {"global_settings": {"mask_secret_ref": _ref()}, "tables": [], "nested": (CANARY,)}
    ),
    "prepare_legacy": lambda p: prepare_legacy(_CanaryKey(), MASK_ROOT),
    "prepare_secret": lambda p: prepare_secret(
        provider=FileSecretProvider(), ref=parse_ref(_ref()), requirements=MASK_ROOT
    ),
}


@pytest.mark.parametrize("route", sorted(ADMISSION))
def test_no_engine_frame_of_a_failing_admission_holds_the_ref(
    route: str, policy: RootRelative
) -> None:
    with scope(policy), pytest.raises(BaseException) as info:
        ADMISSION[route](policy)
    assert isinstance(info.value, (MaskSecretError, OSError, FileAccessUnavailable)) or (
        type(info.value).__name__ == "FileAccessDenied"
    )
    assert_no_engine_local_holds(info.value, CANARY, SHORT_KEY)


class _BoomError(Exception):
    pass


@entry_point
def _failing(path: str, nested: tuple[Any, ...], *, options: Any = None, file_access: Any = None):  # type: ignore[no-untyped-def]
    del path, nested, options
    raise _BoomError


@entry_point
def _failing_gen(path: str, *, file_access: Any = None):  # type: ignore[no-untyped-def]
    del path
    yield 1


def test_the_entry_point_wrapper_keeps_no_argument_when_the_call_fails() -> None:
    with pytest.raises(_BoomError) as info:
        _failing(CANARY, (CANARY, {"k": CANARY}), options={"path": CANARY})
    assert_no_engine_local_holds(info.value, CANARY)


def test_the_generator_wrapper_keeps_no_argument_when_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_entry, "_ambient_forbidden", True)
    token = _entry._CURRENT.set(None)
    try:
        with pytest.raises(FileAccessUnavailable) as info:
            _failing_gen(CANARY)
    finally:
        _entry._CURRENT.reset(token)
    assert_no_engine_local_holds(info.value, CANARY)


def test_the_wrapper_keeps_no_argument_when_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_entry, "_ambient_forbidden", True)
    token = _entry._CURRENT.set(None)
    try:
        with pytest.raises(FileAccessUnavailable) as info:
            _failing(CANARY, (CANARY,), options={"p": CANARY})
    finally:
        _entry._CURRENT.reset(token)
    assert_no_engine_local_holds(info.value, CANARY)


def test_a_context_bound_task_keeps_no_argument_when_it_fails() -> None:
    def work(path: str, *, nested: Any) -> None:
        del path, nested
        raise _BoomError

    with pytest.raises(_BoomError) as info:
        run_in_context(work)(CANARY, nested={"p": (CANARY,)})
    assert_no_engine_local_holds(info.value, CANARY)
