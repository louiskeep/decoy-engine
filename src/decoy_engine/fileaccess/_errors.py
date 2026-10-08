"""Denial hygiene: field-key encoding and boundary scrubbing.

A denial names only a config position, escaped and bounded so a table or column name
carrying control characters cannot forge a log line (CWE-117) and no locator, path,
basename or third-party error text rides with it (CWE-209).
"""

from __future__ import annotations

import errno
import os
import re
from collections.abc import Callable, Iterable, Iterator
from typing import Any, NewType, TypeVar

from decoy_engine.fileaccess._types import (
    FileAccessDenied,
    FileAccessReadError,
    FileAccessUnavailable,
)

EncodedKey = NewType("EncodedKey", str)

_MAX_KEY = 256
_TRUNCATED = "...[truncated]"
T = TypeVar("T")
_RESOURCE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.ENOMEM})

# A descriptor path (`/proc/self/fd/<n>`, `/dev/fd/<n>`) is the locator a strict policy hands a
# library. The specific one is redacted by pair, but a third-party message may name a different
# fd number than the handle we recorded; any descriptor path is a locator, so collapse the whole
# form (CWE-209). No field position is known for an unrecognized fd, so it goes to a fixed marker.
_FD_PATH_RE = re.compile(r"/proc/self/fd/\d+|/dev/fd/\d+")
_FD_REDACTED = "<redacted>"


def encode_field_key(key: str) -> EncodedKey:
    """Backslash-escape non-printables, then bound to 256 characters with a marker."""
    escaped = "".join(ch if ch.isprintable() and ch != "\\" else _escape(ch) for ch in str(key))
    if len(escaped) > _MAX_KEY:
        escaped = escaped[: _MAX_KEY - len(_TRUNCATED)] + _TRUNCATED
    return EncodedKey(escaped)


def _escape(ch: str) -> str:
    return ch.encode("unicode_escape").decode("ascii")


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
            fresh = FileAccessDenied(inner.field_key, encoded=True)
            fresh.__cause__ = None
            fresh.__context__ = None
            fresh.__suppress_context__ = True
            return fresh.with_traceback(None)
        seen.add(id(inner))
        inner = inner.__cause__ or inner.__context__
    return exc


def redact_locators(text: str, pairs: Iterable[tuple[str, str]]) -> str:
    """`text` with each locator replaced by the display name given for it.

    For third-party text that names the file it was handed: under a strict policy that is the
    descriptor path (`/proc/self/fd/<n>`) as well as the original locator. Longest first, so a
    locator that contains another is replaced whole. Where locator and display are equal (the
    ambient policy) nothing changes."""
    out = text
    for locator, display in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        if locator and locator != display:
            out = out.replace(locator, display)
    return _FD_PATH_RE.sub(_FD_REDACTED, out)


def passes_by_type(exc: Exception) -> bool:
    """Failures the caller classifies by TYPE keep their class under a strict policy: memory
    pressure, and a full disk or quota. Masking them as a read failure would break OOM
    handling and disk-quota reporting."""
    if isinstance(exc, MemoryError):
        return True
    if isinstance(exc, OSError) and exc.errno in _RESOURCE_ERRNOS:
        return True
    import duckdb

    return isinstance(exc, duckdb.OutOfMemoryException)


def resurface_scrubbed(exc: Exception) -> Exception | None:
    """A failure the engine recognises only by its TEXT (Arrow's `Wrapping <cell> failed`,
    OpenSSL's context copy), rebuilt as the same type around the scrubbed text; None when `exc`
    is not such a failure or cannot be rebuilt so that the predicate still matches."""
    from decoy_engine.execution._isolated_common import is_memory_failure, scrub_error_text

    if not is_memory_failure(exc):
        return None
    try:
        rebuilt = type(exc)(scrub_error_text(str(exc)))
    except Exception:
        return None
    return rebuilt if is_memory_failure(rebuilt) else None


def _resurface(exc: Exception) -> Exception | None:
    """The exception to raise for a failure the caller must still classify, or None when it
    is an ordinary library failure (a read error).

    By-type failures keep their object, except a resource `OSError`, rebuilt without the
    filename the library puts there. Text-recognised failures go through `resurface_scrubbed`
    because that text can carry a cell value."""
    if isinstance(exc, OSError) and exc.errno in _RESOURCE_ERRNOS:
        return type(exc)(exc.errno, os.strerror(exc.errno or 0))
    if passes_by_type(exc):
        return exc
    return resurface_scrubbed(exc)


def guarded(display: str, strict: bool, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a library call. Under a strict policy a library failure becomes the typed
    `FileAccessReadError` naming only `display`; memory and disk-resource failures keep
    their class; a denial from inside passes unchanged. Under the local policy the call is
    made as it is, so CLI text is unchanged. The new error is raised after the `except`
    block closes so no library error is left as its `__context__`."""
    if not strict:
        return fn(*args, **kwargs)
    resurfaced: Exception | None = None
    try:
        return fn(*args, **kwargs)
    except (FileAccessDenied, FileAccessReadError, FileAccessUnavailable, StopIteration):
        # FileAccessUnavailable is only raised at the policy/open and txn layers today, never by a
        # library read/write of an open handle; passing it through is defensive, not load-bearing.
        raise
    except Exception as exc:
        resurfaced = _resurface(exc)
        if resurfaced is exc:
            raise
    if resurfaced is not None:
        raise resurfaced from None
    raise read_error(display) from None


def read_error(field_key: str) -> FileAccessReadError:
    exc = FileAccessReadError(field_key)
    exc.__suppress_context__ = True
    return exc


def guarded_iter(display: str, strict: bool, it: Iterable[T]) -> Iterator[T]:
    """Step an iterator under `guarded` semantics. Under the local policy it is a plain
    `yield from`, so CLI text is unchanged."""
    if not strict:
        yield from it
        return
    iterator = iter(it)
    while True:
        try:
            item = guarded(display, True, next, iterator)
        except StopIteration:
            return
        yield item
