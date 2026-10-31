"""Decision 2 (A-4 x F13): the admission boundary's closed set of kept classes.

F13 rebuilds every admission failure as a value-free `MaskSecretError`. A-4 requires a strict
policy's decision (`FileAccessDenied`, `FileAccessUnavailable`) and the resource failures
callers classify by type (`MemoryError`, ENOMEM, ENOSPC, EDQUOT) to surface from a `file:` secret
read with their own class. Both hold: for the built-in file provider only, the boundary rebuilds a
FRESH instance of exactly that closed set (fixed text, no chain, no original object); every
other failure, and every failure of any other provider, is still a `MaskSecretError`. Pattern:
the closed enumeration of the F13 boundary itself (`_errors.py`, gRPC status codes / RFC 9457).
"""

from __future__ import annotations

import errno
import os
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import fileaccess
from decoy_engine.fileaccess import (
    FileAccessDenied,
    FileAccessReadError,
    FileAccessUnavailable,
    FileRequest,
)
from decoy_engine.keyprovider import MaskSecretError, key_provider_from_ref
from decoy_engine.secret_providers import (
    API_VERSION,
    SecretDescriptor,
    admit_mask_key,
)
from tests.fileaccess.support import RootRelative

CANARY = "F13xA4-CANARY-/srv/tenant-a/keys/prod.key-hunter2"
FIELD = "global_settings.mask_secret_ref"
GOOD = ("ab" * 32).encode()


class Injecting:
    """A strict policy whose `open_read` raises a prepared, canary-bearing exception."""

    def __init__(self, inner: RootRelative, make: Callable[[], BaseException]) -> None:
        self.inner = inner
        self.make = make
        self.raised: list[BaseException] = []

    def open_read(self, req: FileRequest) -> Any:
        exc = self.make()
        self.raised.append(exc)
        raise exc

    def open_dir(self, req: FileRequest) -> Any:
        return self.inner.open_dir(req)

    def stat(self, req: FileRequest) -> Any:
        return self.inner.stat(req)

    def output_dir(self, req: FileRequest) -> Any:
        return self.inner.output_dir(req)

    def allows_subprocess(self) -> bool:
        return False


@pytest.fixture
def root(tmp_path: Path) -> Iterator[RootRelative]:
    folder = tmp_path / "root"
    folder.mkdir()
    secret = folder / "k.key"
    secret.write_bytes(GOOD)
    os.chmod(secret, 0o600)
    policy = RootRelative(folder)
    try:
        yield policy
    finally:
        policy.close()


def _ref(root: RootRelative) -> str:
    return f"file:{root._root_path}/k.key"


def _via_ref(root: RootRelative, policy: Any) -> Callable[[], Any]:
    return lambda: key_provider_from_ref(_ref(root), file_access=policy)


def _via_admit(root: RootRelative, policy: Any) -> Callable[[], Any]:
    config = {"global_settings": {"mask_secret_ref": _ref(root)}, "tables": []}

    def call() -> Any:
        with fileaccess.scope(policy):
            return admit_mask_key(config)

    return call


ROUTES = {"key_provider_from_ref": _via_ref, "admit_mask_key": _via_admit}

KEPT: dict[str, tuple[Callable[[], BaseException], type[BaseException], int | None]] = {
    "denied": (lambda: FileAccessDenied(CANARY), FileAccessDenied, None),
    "unavailable": (lambda: FileAccessUnavailable(CANARY), FileAccessUnavailable, None),
    "memory": (lambda: MemoryError(CANARY), MemoryError, None),
    "enomem": (lambda: OSError(errno.ENOMEM, CANARY, CANARY), OSError, errno.ENOMEM),
    "enospc": (lambda: OSError(errno.ENOSPC, CANARY, CANARY), OSError, errno.ENOSPC),
    "edquot": (lambda: OSError(errno.EDQUOT, CANARY, CANARY), OSError, errno.EDQUOT),
}

OUTSIDE: dict[str, Callable[[], BaseException]] = {
    "eio": lambda: OSError(errno.EIO, CANARY, CANARY),
    "eacces": lambda: PermissionError(errno.EACCES, CANARY, CANARY),
    "read_error": lambda: FileAccessReadError(CANARY),
    "value_error": lambda: ValueError(CANARY),
    "runtime_error": lambda: RuntimeError(CANARY),
}


def _caught(call: Callable[[], Any]) -> BaseException:
    try:
        call()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        return exc
    raise AssertionError("expected a failure")


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        cur = todo.pop()
        if cur is None or any(cur is s for s in seen):
            continue
        seen.append(cur)
        todo += [cur.__cause__, cur.__context__]
    return seen


def _assert_value_free(exc: BaseException) -> None:
    assert exc.__cause__ is None and exc.__context__ is None
    assert len(_chain(exc)) == 1
    assert not getattr(exc, "__notes__", None)
    texts = [str(exc), repr(exc), repr(exc.args), "".join(traceback.format_exception(exc))]
    texts.append(
        "".join(traceback.TracebackException.from_exception(exc, capture_locals=True).format())
    )
    for text in texts:
        assert "CANARY" not in text and "hunter2" not in text and "tenant-a" not in text, text


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("case", sorted(KEPT))
def test_a_kept_class_is_rebuilt_fresh_and_value_free(root, route: str, case: str) -> None:
    make, cls, code = KEPT[case]
    policy = Injecting(root, make)
    exc = _caught(ROUTES[route](root, policy))
    assert type(exc) is cls, f"{type(exc).__name__}: {exc}"
    assert policy.raised and all(exc is not original for original in policy.raised)
    if code is not None:
        assert isinstance(exc, OSError) and exc.errno == code
        assert exc.filename is None and exc.strerror == os.strerror(code)
    if cls is FileAccessDenied:
        assert str(exc) == f"file access denied: {FIELD}"
    _assert_value_free(exc)


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_real_policy_denial_keeps_its_class(tmp_path: Path, root, route: str) -> None:
    outside = tmp_path / "outside.key"
    outside.write_bytes(GOOD)
    os.chmod(outside, 0o600)
    config = {"global_settings": {"mask_secret_ref": f"file:{outside}"}, "tables": []}
    if route == "key_provider_from_ref":
        call = lambda: key_provider_from_ref(f"file:{outside}", file_access=root)
    else:

        def call() -> Any:
            with fileaccess.scope(root):
                return admit_mask_key(config)

    exc = _caught(call)
    assert type(exc) is FileAccessDenied
    assert str(exc) == f"file access denied: {FIELD}"
    _assert_value_free(exc)


@pytest.mark.parametrize("route", sorted(ROUTES))
@pytest.mark.parametrize("case", sorted(OUTSIDE))
def test_a_class_outside_the_set_is_still_a_mask_secret_error(root, route: str, case: str) -> None:
    policy = Injecting(root, OUTSIDE[case])
    exc = _caught(ROUTES[route](root, policy))
    assert policy.raised, "positive control: the read went through the policy"
    assert isinstance(exc, MaskSecretError), f"{type(exc).__name__} escaped the boundary"
    _assert_value_free(exc)


class _ExtensionRaising:
    """A registered extension provider (not the built-in file provider) raising kept classes."""

    api_version = API_VERSION
    scheme = "ext"

    def __init__(self, make: Callable[[], BaseException]) -> None:
        self.make = make

    def describe(self, ref: Any) -> SecretDescriptor:
        return SecretDescriptor(scheme=self.scheme, key_version="v1")

    def resolve(self, ref: Any, context: Any = None) -> Any:
        raise self.make()

    def preflight(self, ref: Any, requirements: Any, cache_policy: Any) -> Any:
        from decoy_engine.secret_providers import prepare_secret

        return prepare_secret(
            provider=self, ref=ref, requirements=requirements, cache_policy=cache_policy
        )


@pytest.mark.parametrize("case", sorted(KEPT))
def test_another_provider_never_passes_a_kept_class(registry, root, case: str) -> None:
    """The closed set belongs to the built-in file provider: the same classes raised by any
    other provider are rebuilt as `MaskSecretError`, under a strict policy as everywhere."""
    registry.register(_ExtensionRaising(KEPT[case][0]))
    config = {"global_settings": {"mask_secret_ref": "ext:x?encoding=raw"}, "tables": []}
    with fileaccess.scope(root):
        exc = _caught(lambda: admit_mask_key(config, registry=registry))
    assert isinstance(exc, MaskSecretError)
    assert exc.code == "secret_provider_unavailable"
    _assert_value_free(exc)


@pytest.mark.parametrize(
    "make,code",
    [
        (lambda: MemoryError(CANARY), "secret_provider_unavailable"),
        (lambda: OSError(errno.ENOSPC, CANARY, CANARY), "missing_mask_secret"),
    ],
    ids=["memory", "enospc"],
)
def test_the_ambient_policy_keeps_mains_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Callable[[], BaseException], code: str
) -> None:
    """Under the ambient CLI policy nothing is kept (A-4's `keeps_class` is False there), so the
    F13 behavior on main is unchanged."""
    from decoy_engine.fileaccess import LocalFileAccess

    called: list[bool] = []

    def boom(self: Any, req: FileRequest) -> Any:
        called.append(True)
        raise make()

    monkeypatch.setattr(LocalFileAccess, "open_read", boom)
    secret = tmp_path / "k.key"
    secret.write_bytes(GOOD)
    os.chmod(secret, 0o600)
    exc = _caught(lambda: key_provider_from_ref(f"file:{secret}"))
    assert called, "positive control: the ambient read went through the policy"
    assert isinstance(exc, MaskSecretError) and exc.code == code
    _assert_value_free(exc)


def test_another_provider_cannot_raise_the_carrier(registry, root) -> None:
    """The carrier is honored only from the built-in file provider: another provider raising it
    (by mistake or by copying the engine's code) gets `secret_provider_unavailable`."""
    from decoy_engine.secret_providers._errors import KeptFileFailure

    registry.register(_ExtensionRaising(lambda: KeptFileFailure("file_access_denied")))
    config = {"global_settings": {"mask_secret_ref": "ext:x?encoding=raw"}, "tables": []}
    with fileaccess.scope(root):
        exc = _caught(lambda: admit_mask_key(config, registry=registry))
    assert isinstance(exc, MaskSecretError) and exc.code == "secret_provider_unavailable"
    _assert_value_free(exc)
