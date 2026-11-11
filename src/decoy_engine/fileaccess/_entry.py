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
from typing import Any, NoReturn, TypeVar

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


def _unavailable(message: str) -> NoReturn:
    """Raise `FileAccessUnavailable` with no inherited chain: a caller may resolve a policy
    while handling its own exception (a decode error holding secret bytes, say), which Python
    would otherwise attach as `__context__`."""
    try:
        raise FileAccessUnavailable(message)
    except FileAccessUnavailable as fresh:
        fresh.__cause__ = None
        fresh.__context__ = None
        fresh.__suppress_context__ = True
        raise


class _Arguments:
    """A call's arguments, held where a traceback keeps the frame: the repr is fixed, so
    `TracebackException(capture_locals=True)` shows no ref, path or value a caller passed."""

    __slots__ = ("args", "kwargs")

    def __init__(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.args = args
        self.kwargs = kwargs

    def __repr__(self) -> str:
        return "<arguments>"

    __str__ = __repr__

    def __reduce_ex__(self, *_: object) -> Any:
        raise TypeError("call arguments cannot be pickled")


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
        _unavailable("no file access policy is installed")
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
        _unavailable(f"{what} needs the ambient file access policy")


def _note(policy: FileAccess) -> FileAccess:
    global _strict_in_use
    if not isinstance(policy, LocalFileAccess):
        _strict_in_use = True
    return policy


def _reset_for_tests() -> None:
    """Return the process to its default state (test isolation only)."""
    global _strict_in_use
    _strict_in_use = False


def reset_for_tests() -> None:
    """Test isolation for hosts: lift both one-way latches (`forbid_ambient()` and the
    strict-policy-in-use latch). Production code never calls this."""
    global _ambient_forbidden
    _ambient_forbidden = False
    _reset_for_tests()


def _not_broader(explicit: FileAccess, installed: FileAccess) -> bool:
    """The only ordering policies have: the ambient CLI policy is the widest, and two strict
    policies are incomparable unless they are the same object."""
    return explicit is installed or isinstance(installed, LocalFileAccess)


def _resolve(explicit: FileAccess | None) -> FileAccess:
    existing = _CURRENT.get()
    if explicit is not None:
        if _ambient_forbidden and isinstance(explicit, LocalFileAccess):
            _unavailable("the ambient policy is forbidden in this process")
        # A nested call never widens the installed policy, even by naming one.
        if existing is not None and not _not_broader(explicit, existing):
            _unavailable("an explicit file access policy may not widen the installed one")
        return _note(explicit)
    if existing is not None:
        return existing
    if _ambient_forbidden or _strict_in_use:
        _unavailable("no file access policy was given and ambient access is forbidden")
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


# The iterator each live `_BoundIterator` wraps, keyed by the wrapper's id. Python has no
# finalizer hook for sync generators (PEP 525 gives one only to async generators), so when a
# wrapper and its generator die together in a cycle the collector may finalize the generator
# first, running its `finally` in whatever context is current. Holding the generator here
# keeps it out of the cycle until the wrapper's `__del__` has closed it in its own context.
_ANCHORS: dict[int, Iterator[Any]] = {}


class _BoundIterator:
    """Runs every step of an iterator in one private context, copied when it was bound and
    holding the policy it was bound under (the `asyncio.Task` pattern from PEP 567).

    A ContextVar token the iterator creates before a yield belongs to that context, so its
    reset works on any later step, including a close from the garbage collector. A close
    that set and reset variables on the current context instead could run inside another
    `ContextVar.set` on this thread and free the map that call is still reading (a segfault
    on CPython 3.10). Values the consumer sets after binding are not seen by the iterator,
    and values the iterator sets do not reach the consumer.
    """

    def __init__(self, policy: FileAccess, it: Iterable[Any]) -> None:
        self._ctx = contextvars.copy_context()
        self._ctx.run(_CURRENT.set, policy)
        self._it = self._ctx.run(iter, it)
        _ANCHORS[id(self)] = self._it

    def __iter__(self) -> _BoundIterator:
        return self

    def _step(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return self._ctx.run(fn, *args)
        except StopIteration:
            _ANCHORS.pop(id(self), None)
            raise

    def __next__(self) -> Any:
        return self._step(next, self._it)

    def send(self, value: Any) -> Any:
        return self._step(self._it.send, value)  # type: ignore[attr-defined]

    def throw(self, *args: Any) -> Any:
        return self._step(self._it.throw, *args)  # type: ignore[attr-defined]

    def close(self) -> None:
        try:
            closer = getattr(self._it, "close", None)
            if closer is not None:
                self._ctx.run(closer)
        finally:
            _ANCHORS.pop(id(self), None)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def bind_iterator(policy: FileAccess, it: Iterable[T]) -> Iterator[T]:
    """Wrap a generator or iterator so every `__next__`, `send`, `throw` and `close` runs
    under `policy`, whatever is installed when it is consumed, in a private copy of the
    context taken now."""
    return _BoundIterator(policy, it)


def run_in_context(fn: Callable[..., T]) -> Callable[..., T]:
    """A callable that runs `fn` in a fresh `copy_context()` taken now. Call it once per
    submitted task: a `Context` cannot be entered by two tasks at once."""
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def task(*args: Any, **kwargs: Any) -> T:
        call = _Arguments(args, kwargs)
        del args, kwargs
        return ctx.run(fn, *call.args, **call.kwargs)

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

    # Both wrappers move the caller's arguments into `_Arguments` before anything can raise,
    # and unpack them only inside the call expression, so no wrapper frame on a traceback
    # holds a ref, path or value in a local.
    if inspect.isgeneratorfunction(fn):

        @functools.wraps(fn)
        def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
            call = _Arguments(args, kwargs)
            del args, kwargs
            return bind_iterator(
                _resolve(call.kwargs.get("file_access")), fn(*call.args, **call.kwargs)
            )

        gen_wrapper.__decoy_entry_point__ = True  # type: ignore[attr-defined]
        return gen_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        call = _Arguments(args, kwargs)
        del args, kwargs
        token = _CURRENT.set(_resolve(call.kwargs.get("file_access")))
        try:
            result = fn(*call.args, **call.kwargs)
            if isinstance(result, Iterator):
                return bind_iterator(_CURRENT.get(), result)  # type: ignore[arg-type]
            return result
        finally:
            _CURRENT.reset(token)

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
