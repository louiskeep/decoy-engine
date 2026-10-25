"""Entry-point plumbing: the installed policy, `forbid_ambient`, iterator and thread binding.

Pattern: contextvars (PEP 567) as the lookup for synchronous code inside a call; authority
that outlives a call (lazy iterators, writers, pool tasks) is bound explicitly to the policy
it was created under, never found through the variable later (docs/security/
file-access-policy.md).
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from decoy_engine.fileaccess._local import LocalFileAccess
from decoy_engine.fileaccess._types import FileAccess, FileAccessUnavailable

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])

_CURRENT: contextvars.ContextVar[FileAccess | None] = contextvars.ContextVar(
    "decoy_file_access", default=None
)
_POSITION: contextvars.ContextVar[str] = contextvars.ContextVar("decoy_file_position", default="")
_ambient_forbidden = False
# Set the first time a non-ambient policy is installed or captured: from then on a context
# with no policy (a raw thread, a callback) fails closed instead of falling back to the CLI
# policy, since the process is evidently serving policy-bound work.
_strict_in_use = False


def forbid_ambient() -> None:
    """One-way for the process: an entry point called without a policy now raises
    `FileAccessUnavailable` instead of falling back to the permissive CLI policy."""
    global _ambient_forbidden
    _ambient_forbidden = True


def ambient_forbidden() -> bool:
    return _ambient_forbidden


def current() -> FileAccess:
    """The policy internal code reads files through: the installed one, else the ambient CLI
    policy, unless the process called `forbid_ambient()`, in which case nothing installed
    raises. Platform processes forbid ambient access at startup, so for them a call that
    reaches the file system with no policy fails closed instead of falling open."""
    policy = _CURRENT.get()
    if policy is not None:
        return policy
    if _ambient_forbidden or _strict_in_use:
        raise FileAccessUnavailable("no file access policy is installed")
    return _ambient()


_AMBIENT: LocalFileAccess | None = None


def _ambient() -> LocalFileAccess:
    global _AMBIENT
    if _AMBIENT is None:
        _AMBIENT = LocalFileAccess()
    return _AMBIENT


def installed() -> FileAccess | None:
    return _CURRENT.get()


def is_ambient(policy: FileAccess | None = None) -> bool:
    """True when `policy` (default: the installed one) is the ambient CLI policy, the only
    one that may take a caller-supplied path string where a capability would be required."""
    if policy is None:
        policy = _CURRENT.get()
        if policy is None:
            return not (_ambient_forbidden or _strict_in_use)
    return isinstance(policy, LocalFileAccess)


def require_ambient(what: str) -> None:
    """Refuse `what` under any policy but the ambient CLI one.

    For the few engine routes that act on a caller-named scratch or staging path: a strict
    policy cannot vouch for such a path, so the route fails closed before any I/O."""
    if not is_ambient():
        raise FileAccessUnavailable(f"{what} needs the ambient file access policy")


def _note(policy: FileAccess) -> FileAccess:
    global _strict_in_use
    if not isinstance(policy, LocalFileAccess):
        _strict_in_use = True
    return policy


def _reset_for_tests() -> None:
    """Return the process to its default state (test isolation only)."""
    global _strict_in_use
    _strict_in_use = False


def _resolve(explicit: FileAccess | None) -> FileAccess:
    if explicit is not None:
        if _ambient_forbidden and isinstance(explicit, LocalFileAccess):
            raise FileAccessUnavailable("the ambient policy is forbidden in this process")
        return _note(explicit)
    existing = _CURRENT.get()
    if existing is not None:
        return existing
    if _ambient_forbidden or _strict_in_use:
        raise FileAccessUnavailable(
            "no file access policy was given and ambient access is forbidden"
        )
    return _ambient()


def capture(explicit: FileAccess | None = None) -> FileAccess:
    """The policy a public object that does I/O later (a lazy source, a sink, a writer)
    binds at construction: the one given, else the installed one, else the ambient CLI
    policy unless the process forbade it."""
    return _resolve(explicit)


@contextmanager
def scope(file_access: FileAccess | None = None) -> Iterator[FileAccess]:
    """Install the resolved policy for the duration of the block."""
    policy = _resolve(file_access)
    token = _CURRENT.set(policy)
    try:
        yield policy
    finally:
        _CURRENT.reset(token)


@contextmanager
def at_position(path: str) -> Iterator[None]:
    """Name the config position (for example `tables.orders.columns.zip`) that the reads in
    the block belong to, so a denial can say which field asked. A host that validates one
    column at a time wraps its call in this; the engine does so where it walks a config."""
    token = _POSITION.set(path)
    try:
        yield
    finally:
        _POSITION.reset(token)


def position_key(suffix: str) -> str:
    """`<position>.<suffix>` inside `at_position`, else just `suffix`."""
    base = _POSITION.get()
    return f"{base}.{suffix}" if base else suffix


class _BoundIterator:
    """Runs every step of an iterator inside the policy it was created under."""

    def __init__(self, policy: FileAccess, it: Iterable[Any]) -> None:
        self._policy = policy
        self._it = iter(it)

    def __iter__(self) -> _BoundIterator:
        return self

    def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        token = _CURRENT.set(self._policy)
        try:
            return fn(*args)
        finally:
            _CURRENT.reset(token)

    def __next__(self) -> Any:
        return self._run(next, self._it)

    def send(self, value: Any) -> Any:
        return self._run(self._it.send, value)  # type: ignore[attr-defined]

    def throw(self, *args: Any) -> Any:
        return self._run(self._it.throw, *args)  # type: ignore[attr-defined]

    def close(self) -> None:
        closer = getattr(self._it, "close", None)
        if closer is not None:
            self._run(closer)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def bind_iterator(policy: FileAccess, it: Iterable[T]) -> Iterator[T]:
    """Wrap a generator or iterator so every `__next__`, `send`, `throw` and `close` runs
    under `policy`, whatever is installed when it is consumed."""
    return _BoundIterator(policy, it)


def run_in_context(fn: Callable[..., T]) -> Callable[..., T]:
    """A callable that runs `fn` in a fresh `copy_context()` taken now. Call it once per
    submitted task: a `Context` cannot be entered by two tasks at once."""
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def task(*args: Any, **kwargs: Any) -> T:
        return ctx.run(fn, *args, **kwargs)

    return task


def map_in_context(executor: Any, fn: Callable[[Any], T], items: Iterable[Any]) -> Iterator[T]:
    """`executor.map` with one fresh context per task, results in input order."""
    futures = [executor.submit(run_in_context(fn), item) for item in items]
    for future in futures:
        yield future.result()


def entry_point(fn: F) -> F:
    """Mark a public callable that takes `file_access=`: it installs the resolved policy for
    the call, and a generator result keeps that policy for every later step."""
    if "file_access" not in inspect.signature(fn).parameters:
        raise TypeError(f"{fn.__qualname__} must accept file_access=")

    if inspect.isgeneratorfunction(fn):

        @functools.wraps(fn)
        def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
            policy = _resolve(kwargs.get("file_access"))
            return bind_iterator(policy, fn(*args, **kwargs))

        gen_wrapper.__decoy_entry_point__ = True  # type: ignore[attr-defined]
        return gen_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with scope(kwargs.get("file_access")) as policy:
            result = fn(*args, **kwargs)
        if isinstance(result, Iterator):
            return bind_iterator(policy, result)
        return result

    wrapper.__decoy_entry_point__ = True  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]


__all__ = [
    "ambient_forbidden",
    "at_position",
    "bind_iterator",
    "capture",
    "current",
    "entry_point",
    "forbid_ambient",
    "installed",
    "is_ambient",
    "map_in_context",
    "position_key",
    "require_ambient",
    "run_in_context",
    "scope",
]
