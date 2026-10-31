"""AT-X1: secret material never survives in an error, with or without a caller's active handler."""

from __future__ import annotations

import errno
import logging
import traceback
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileAccessDenied
from decoy_engine.keyprovider import (
    MaskSecretError,
    MissingMaskSecret,
    SecretKeyProvider,
    key_provider_from_ref,
)
from tests.fileaccess.support import RootRelative

SENTINEL = "SECRET-" + uuid.uuid4().hex[:10]
GOOD = "ab" * 32

CASES = {
    "invalid_utf8": SENTINEL.encode() + b"\xff\xfe" + b"x" * 40,
    "bad_hex": (SENTINEL + "-zz-not-hex").encode(),
    "bad_base64": (SENTINEL + "A").encode(),
    "non_ascii_utf8": (SENTINEL + "-éè").encode("utf-8"),
}


@pytest.fixture
def policy(tmp_path: Path) -> Iterator[RootRelative]:
    (tmp_path / "root").mkdir()
    p = RootRelative(tmp_path / "root")
    try:
        yield p
    finally:
        p.close()


def _secret_file(policy: RootRelative, data: bytes) -> str:
    path = Path(policy._root_path) / f"s-{uuid.uuid4().hex[:6]}.key"
    path.write_bytes(data)
    path.chmod(0o600)  # E1: a file: secret must be owner-only
    return f"file:{path}"


def _decoded(text: str) -> bytes:
    # The admitted mask key of material `text` (hex): the same derivation as a direct provider.
    return SecretKeyProvider(bytes.fromhex(text)).mask_key()


def _links(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        stack.extend([cur.__cause__, cur.__context__])


def _renders(exc: BaseException) -> list[str]:
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(logging.Formatter().format(record))

    log = logging.getLogger(f"x1.{uuid.uuid4().hex}")
    log.addHandler(Capture())
    log.propagate = False
    log.error("secret resolution failed", exc_info=exc)
    return [
        str(exc),
        repr(exc),
        "".join(traceback.format_exception(exc)),
        *records,
        repr(exc.args),
        repr(getattr(exc, "__notes__", None)),
    ]


def _assert_clean(exc: BaseException, *, secret_path: str) -> None:
    for text in _renders(exc):
        assert SENTINEL not in text and secret_path not in text, text
    assert not hasattr(exc, "__notes__")
    assert exc.__cause__ is None and exc.__context__ is None
    for link in _links(exc):
        assert not isinstance(link, UnicodeDecodeError)
        assert SENTINEL not in repr(getattr(link, "object", ""))
    # Frame locals of engine frames on the traceback never hold the secret text.
    tb = exc.__traceback__
    while tb is not None:
        engine_frame = "/src/decoy_engine/" in tb.tb_frame.f_code.co_filename
        for value in tb.tb_frame.f_locals.values() if engine_frame else ():
            if isinstance(value, (str, bytes)):
                assert SENTINEL not in (value if isinstance(value, str) else value.decode("latin1"))
        tb = tb.tb_next


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_bad_secret_leaves_nothing_in_the_error(policy: RootRelative, name: str) -> None:
    ref = _secret_file(policy, CASES[name])
    with pytest.raises(MaskSecretError) as info:
        key_provider_from_ref(ref, file_access=policy).mask_key()
    _assert_clean(info.value, secret_path=ref[len("file:") :])


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_same_inside_a_callers_active_handler(policy: RootRelative, name: str) -> None:
    """A caller already handling a secret-bearing decode error must not become the context."""
    ref = _secret_file(policy, CASES[name])
    carrier = SENTINEL.encode() + b"\xff"
    try:
        carrier.decode("utf-8")
    except UnicodeDecodeError:
        with pytest.raises(MaskSecretError) as info:
            key_provider_from_ref(ref, file_access=policy).mask_key()
    _assert_clean(info.value, secret_path=ref[len("file:") :])


def test_a_weak_secret_inside_a_handler_leaves_nothing(policy: RootRelative) -> None:
    ref = _secret_file(policy, b"ab" * 4)
    carrier = SENTINEL.encode() + b"\xff"
    try:
        carrier.decode("utf-8")
    except UnicodeDecodeError:
        with pytest.raises(MaskSecretError) as info:
            key_provider_from_ref(ref, file_access=policy)
    assert info.value.code == "weak_mask_secret"
    _assert_clean(info.value, secret_path=ref[len("file:") :])


def test_a_good_secret_still_resolves_strictly(policy: RootRelative) -> None:
    ref = _secret_file(policy, GOOD.encode())
    assert key_provider_from_ref(ref, file_access=policy).mask_key() == _decoded(GOOD)


def test_a_policy_denial_keeps_its_class(policy: RootRelative, tmp_path: Path) -> None:
    outside = tmp_path / "outside.key"
    outside.write_text(GOOD)
    outside.chmod(0o600)  # E1: a file: secret must be owner-only
    with pytest.raises(FileAccessDenied):
        key_provider_from_ref(f"file:{outside}", file_access=policy).mask_key()


def test_unavailable_keeps_its_class(monkeypatch: pytest.MonkeyPatch, policy: RootRelative) -> None:
    def boom(*a: Any, **k: Any) -> str:
        raise fileaccess.FileAccessUnavailable("no")

    monkeypatch.setattr(fileaccess, "read_checked", boom)
    with pytest.raises(fileaccess.FileAccessUnavailable):
        key_provider_from_ref("file:/x", file_access=policy).mask_key()


@pytest.mark.parametrize(
    "injected",
    [
        MemoryError("x"),
        OSError(errno.ENOMEM, "nomem"),
        OSError(errno.ENOSPC, "nospc"),
        OSError(errno.EDQUOT, "quota"),
    ],
    ids=["memory", "enomem", "enospc", "edquot"],
)
def test_resource_failures_during_the_read_keep_their_class(
    monkeypatch: pytest.MonkeyPatch, policy: RootRelative, injected: BaseException
) -> None:
    def boom(*a: Any, **k: Any) -> str:
        raise injected

    monkeypatch.setattr(fileaccess, "read_checked", boom)
    with pytest.raises(type(injected)) as info:
        key_provider_from_ref("file:/x", file_access=policy).mask_key()
    if isinstance(injected, OSError):
        assert info.value.errno == injected.errno
    assert not isinstance(info.value, MissingMaskSecret)


def test_an_ordinary_read_failure_is_still_the_missing_secret_error(
    monkeypatch: pytest.MonkeyPatch, policy: RootRelative
) -> None:
    def boom(*a: Any, **k: Any) -> str:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(fileaccess, "read_checked", boom)
    with pytest.raises(MissingMaskSecret) as info:
        key_provider_from_ref("file:/x", file_access=policy).mask_key()
    assert "/x" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


# -- compatibility controls: the ambient policy keeps today's behavior ------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_ambient_behavior_is_unchanged(tmp_path: Path, name: str) -> None:
    path = tmp_path / "ambient.key"
    path.write_bytes(CASES[name])
    path.chmod(0o600)  # E1: a file: secret must be owner-only
    with pytest.raises(MaskSecretError) as info:
        key_provider_from_ref(f"file:{path}").mask_key()
    assert info.value.code == "bad_secret_ref"


def test_ambient_good_secret_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "ok.key"
    path.write_text(GOOD)
    path.chmod(0o600)  # E1: a file: secret must be owner-only
    assert key_provider_from_ref(f"file:{path}").mask_key() == _decoded(GOOD)
    with pytest.raises(MissingMaskSecret):
        key_provider_from_ref(f"file:{tmp_path / 'nope'}").mask_key()
