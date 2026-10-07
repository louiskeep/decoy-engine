"""Denial hygiene: field-key encoding and boundary scrubbing.

A denial names only a config position, escaped and bounded so a table or column name
carrying control characters cannot forge a log line (CWE-117) and no locator, path,
basename or third-party error text rides with it (CWE-209).
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable, Iterator
from typing import Any, NewType, TypeVar

from decoy_engine.fileaccess._types import FileAccessDenied

EncodedKey = NewType("EncodedKey", str)

_MAX_KEY = 256
_TRUNCATED = "...[truncated]"
T = TypeVar("T")


def encode_field_key(key: str) -> EncodedKey:
    """Backslash-escape non-printables, then bound to 256 characters with a marker."""
    escaped = "".join(ch if ch.isprintable() and ch != "\\" else _escape(ch) for ch in str(key))
    if len(escaped) > _MAX_KEY:
        escaped = escaped[: _MAX_KEY - len(_TRUNCATED)] + _TRUNCATED
    return EncodedKey(escaped)


def _escape(ch: str) -> str:
    if ch == "\\":
        return "\\\\"
    code = ord(ch)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def deny(field_key: str) -> FileAccessDenied:
    """A fresh denial with nothing chained. Build it outside any `except` that holds a
    library error, and raise it `from None`."""
    exc = FileAccessDenied(field_key)
    exc.__suppress_context__ = True
    return exc


def has_denial(exc: BaseException | None) -> bool:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, FileAccessDenied):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def scrub(exc: BaseException) -> BaseException:
    """The exception that may cross a process, API or log boundary.

    A chain that holds a denial becomes one fresh denial with no cause, context or
    traceback (so no frame locals are reachable); anything else is returned as it is."""
    inner: BaseException | None = exc
    seen: set[int] = set()
    while inner is not None and id(inner) not in seen:
        if isinstance(inner, FileAccessDenied):
            fresh = FileAccessDenied(inner.field_key)
            fresh.__cause__ = None
            fresh.__context__ = None
            fresh.__suppress_context__ = True
            return fresh.with_traceback(None)
        seen.add(id(inner))
        inner = inner.__cause__ or inner.__context__
    return exc


def guarded(display: str, strict: bool, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a library call. Under a strict policy any library exception or warning becomes a
    denial naming only `display`; under the local policy the call is made as it is, so CLI
    text is unchanged. The denial is raised after the `except` block closes so no library
    error is left as its `__context__`."""
    if not strict:
        return fn(*args, **kwargs)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return fn(*args, **kwargs)
        except FileAccessDenied:
            raise
        except Exception:
            pass
    raise deny(display) from None


def guarded_iter(display: str, strict: bool, it: Iterable[T]) -> Iterator[T]:
    """Step an iterator under `guarded` semantics: each step's library failure becomes the
    denial. Under the local policy it is a plain `yield from`, so CLI text is unchanged."""
    if not strict:
        yield from it
        return
    iterator = iter(it)
    while True:
        failed = False
        done = False
        item: T | None = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                item = next(iterator)
            except StopIteration:
                done = True
            except FileAccessDenied:
                raise
            except Exception:
                failed = True
        if failed:
            raise deny(display) from None
        if done:
            return
        yield item  # type: ignore[misc]
