"""AT-E2, AT-E11, AT-E15, AT-E16, AT-E17: installation, binding, hygiene."""

from __future__ import annotations

import concurrent.futures
import contextvars
import subprocess
import sys
import textwrap
import threading
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import fileaccess
from decoy_engine.fileaccess import (
    FileAccessDenied,
    FileRequest,
    FileRole,
    LocalFileAccess,
    RootedFileAccess,
    bind_iterator,
    current,
    entry_point,
    run_in_context,
    scope,
)
from decoy_engine.fileaccess._entry import _CURRENT
from tests.fileaccess.conftest import READ_ROLES  # noqa: F401  (fixture module import)

TOKEN = "tok-9d41c0aa77e2"


class _Marker:
    """A policy that only identifies itself; enough to test who is installed."""

    def __init__(self, name: str) -> None:
        self.name = name

    def allows_subprocess(self) -> bool:
        return False


@pytest.fixture
def nothing_installed() -> Iterator[None]:
    token = _CURRENT.set(None)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def test_internal_code_with_nothing_installed_falls_back_to_ambient_unless_forbidden(
    nothing_installed: None,
) -> None:
    # The fallback is the CLI's ambient policy and is stateless; a process that called
    # `forbid_ambient()` gets the refusal instead (see the subprocess test below).
    assert isinstance(current(), LocalFileAccess)
    assert fileaccess.installed() is None


def test_entry_point_installs_for_the_call_and_never_downgrades(nothing_installed: None) -> None:
    seen: dict[str, Any] = {}

    @entry_point
    def inner(*, file_access: Any = None) -> None:
        seen["inner"] = current()

    @entry_point
    def outer(*, file_access: Any = None) -> None:
        seen["outer"] = current()
        inner()

    strict = _Marker("strict")
    outer(file_access=strict)  # type: ignore[arg-type]
    assert seen["outer"] is strict and seen["inner"] is strict
    outer()
    assert isinstance(seen["outer"], LocalFileAccess) and isinstance(seen["inner"], LocalFileAccess)
    assert fileaccess.installed() is None


def test_entry_point_requires_the_keyword() -> None:
    with pytest.raises(TypeError):

        @entry_point
        def bad() -> None: ...


def test_forbid_ambient_is_one_way_and_fails_before_any_syscall() -> None:
    code = textwrap.dedent(
        """
        from decoy_engine import fileaccess
        from decoy_engine.fileaccess import FileAccessUnavailable, LocalFileAccess, entry_point

        calls = []

        @entry_point
        def ep(*, file_access=None):
            calls.append(1)

        fileaccess.forbid_ambient()
        for attempt in (lambda: ep(), lambda: ep(file_access=LocalFileAccess())):
            try:
                attempt()
            except FileAccessUnavailable:
                pass
            else:
                raise SystemExit("did not raise")
        assert calls == []
        class Strict:
            def allows_subprocess(self): return False
        ep(file_access=Strict())
        assert calls == [1]
        try:
            fileaccess.current()
        except FileAccessUnavailable:
            print("ok")
        """
    )
    out = subprocess.run(  # noqa: S603 - runs this interpreter on a fixed script
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def test_bound_iterator_uses_its_own_policy_whatever_is_installed(nothing_installed: None) -> None:
    a, b = _Marker("a"), _Marker("b")

    def gen(label: str) -> Iterator[tuple[str, str]]:
        for _ in range(3):
            yield label, current().name  # type: ignore[attr-defined]

    with scope(a):  # type: ignore[arg-type]
        it_a = bind_iterator(a, gen("a"))  # type: ignore[arg-type]
    it_b = bind_iterator(b, gen("b"))  # type: ignore[arg-type]
    assert next(it_a) == ("a", "a")
    with scope(_Marker("other")):  # type: ignore[arg-type]
        assert next(it_a) == ("a", "a")
        assert next(it_b) == ("b", "b")
        assert next(it_a) == ("a", "a")
    with pytest.raises(StopIteration):
        next(it_a)
    assert fileaccess.installed() is None


def test_bound_iterator_nested_and_threaded() -> None:
    a, b = _Marker("a"), _Marker("b")

    def gen(n: int) -> Iterator[str]:
        for _ in range(n):
            yield current().name  # type: ignore[attr-defined]

    outer = bind_iterator(a, gen(4))  # type: ignore[arg-type]
    inner = bind_iterator(b, gen(4))  # type: ignore[arg-type]
    seen = []
    for _ in range(2):
        seen.append(next(outer))
        seen.append(next(inner))
    assert seen == ["a", "b", "a", "b"]
    results: dict[str, list[str]] = {}

    def consume(name: str, it: Iterator[str]) -> None:
        results[name] = list(it)

    threads = [
        threading.Thread(target=consume, args=("a", bind_iterator(a, gen(50)))),  # type: ignore[arg-type]
        threading.Thread(target=consume, args=("b", bind_iterator(b, gen(50)))),  # type: ignore[arg-type]
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results["a"] == ["a"] * 50 and results["b"] == ["b"] * 50


def test_bound_iterator_closes_the_generator_under_its_policy() -> None:
    closed: list[str] = []

    def gen() -> Iterator[int]:
        try:
            yield 1
            yield 2
        finally:
            closed.append(current().name)  # type: ignore[attr-defined]

    it = bind_iterator(_Marker("a"), gen())  # type: ignore[arg-type]
    assert next(it) == 1
    it.close()  # type: ignore[attr-defined]
    assert closed == ["a"]


def test_entry_point_generator_keeps_its_policy(nothing_installed: None) -> None:
    @entry_point
    def stream(n: int, *, file_access: Any = None) -> Iterator[str]:
        for _ in range(n):
            yield current().name  # type: ignore[attr-defined]

    it = stream(3, file_access=_Marker("strict"))
    with scope(_Marker("later")):  # type: ignore[arg-type]
        assert list(it) == ["strict"] * 3


def test_run_in_context_gives_each_task_a_fresh_context() -> None:
    var: contextvars.ContextVar[int] = contextvars.ContextVar("v", default=0)
    seen: list[int] = []

    def task(i: int) -> int:
        var.set(var.get() + i)
        return var.get()

    token = var.set(100)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(run_in_context(task), i) for i in range(64)]
            seen = [f.result() for f in futures]
    finally:
        var.reset(token)
    assert seen == [100 + i for i in range(64)]
    assert var.get() == 0


def test_policy_set_before_submission_reaches_pool_workers(nothing_installed: None) -> None:
    with scope(_Marker("strict")):  # type: ignore[arg-type]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            names = list(
                fileaccess.map_in_context(pool, lambda _: current().name, range(5))  # type: ignore[attr-defined]
            )
    assert names == ["strict"] * 5
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        with scope(_Marker("strict")):  # type: ignore[arg-type]
            raw = pool.submit(lambda: _CURRENT.get())
        assert raw.result() is None


def _denial(rooted: RootedFileAccess, locator: str, field_key: str) -> FileAccessDenied:
    try:
        rooted.open_read(FileRequest(FileRole.SOURCE, "read", locator, field_key))
    except FileAccessDenied as exc:
        return exc
    raise AssertionError("not denied")


@pytest.mark.parametrize(
    "locator",
    [f"../{TOKEN}", f"/{TOKEN}/x", f"{TOKEN}.parquet", f"a/{TOKEN}/../../../x", f"x\0{TOKEN}"],
)
def test_denial_object_carries_no_locator(rooted: RootedFileAccess, locator: str) -> None:
    exc = _denial(rooted, locator, "tables.t.columns.c.snapshot_file")
    assert str(exc) == "file access denied: tables.t.columns.c.snapshot_file"
    assert exc.__cause__ is None and exc.__suppress_context__ and exc.__context__ is None
    text = "".join(traceback.format_exception(exc))
    assert TOKEN not in text and TOKEN not in repr(exc)
    tb = exc.__traceback__
    while tb is not None:
        for value in tb.tb_frame.f_locals.values():
            if isinstance(value, FileRequest):
                continue  # the request is the caller's own object, never in the message
        tb = tb.tb_next
    import pickle

    assert str(pickle.loads(pickle.dumps(exc))) == str(exc)  # noqa: S301 - our own exception


def test_scrub_replaces_a_chain_holding_a_denial() -> None:
    from decoy_engine.fileaccess import scrub

    try:
        try:
            raise FileAccessDenied("k.a")
        except FileAccessDenied as inner:
            raise RuntimeError(f"wrapped {TOKEN}") from inner
    except RuntimeError as outer:
        fresh = scrub(outer)
    assert isinstance(fresh, FileAccessDenied) and str(fresh) == "file access denied: k.a"
    assert fresh.__cause__ is None and fresh.__context__ is None and fresh.__traceback__ is None
    plain = ValueError("x")
    assert scrub(plain) is plain


def test_field_key_is_escaped_and_bounded() -> None:
    key = "tables.t\ncolumns.c\r\x1b[31m" + "x" * 4000
    exc = FileAccessDenied(key)
    text = str(exc)
    assert "\n" not in text and "\r" not in text and "\x1b" not in text
    assert "\\n" in text and "\\r" in text and "\\x1b" in text
    assert len(exc.field_key) <= 256 and exc.field_key.endswith("[truncated]")
    assert len(FileAccessDenied("short.key").field_key) == len("short.key")


def test_library_errors_become_the_denial_under_a_strict_handle(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    import warnings

    import pyarrow.parquet  # noqa: F401

    from decoy_engine.fileaccess.adapters import arrow

    (root_dir / "bad.parquet").write_text(f"not parquet {TOKEN}")
    handle = rooted.open_read(FileRequest(FileRole.SOURCE, "read", "bad.parquet", "sources.t.path"))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(FileAccessDenied) as info:
            arrow.read_table(handle)
    assert str(info.value) == "file access denied: sources.t.path"
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert TOKEN not in "".join(traceback.format_exception(info.value))


def test_a_nested_entry_point_call_does_not_downgrade_a_strict_policy(
    nothing_installed: None,
) -> None:
    seen: list[object] = []

    @entry_point
    def leaf(*, file_access: Any = None) -> None:
        seen.append(current())

    @entry_point
    def middle(*, file_access: Any = None) -> None:
        leaf(file_access=None)
        leaf()

    strict = _Marker("strict")
    middle(file_access=strict)  # type: ignore[arg-type]
    assert seen == [strict, strict]
