"""One canonical JSON owner for every digest, signature and seed fingerprint.

Both forms sort keys, use compact separators, refuse NaN and Infinity and write UTF-8. They differ
in one argument, ``ensure_ascii``:

* ``canonical_json_bytes`` writes text as-is. This is the records rule (and the platform's), so a
  record id and the CLI evidence manifest hash agree on bytes. Every digest uses it.
* ``seed_fingerprint_bytes`` escapes non-ASCII text. Two seed inputs were hashed that way before
  this module existed, so their bytes are frozen under ``SEED_PROTOCOL_VERSION``: changing them
  would move every generated column for a non-ASCII config.

The encoder never converts silently. A ``date`` and a timezone-aware ``datetime`` become
``isoformat()``; a naive ``datetime`` is refused because reading it as host time would make the
bytes depend on the machine; every other non-JSON type is refused. An unpaired surrogate in a
string or an object key is refused before encoding: the ASCII form would otherwise escape it and
the UTF-8 form would crash on it with codec text.

Every refusal is raised after the encoder or the walk has returned, outside any ``except`` block,
so the error holds no ``__context__`` and no text of the value.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from decoy_engine import reasons
from decoy_engine._error_base import DecoyError

__all__ = [
    "CanonicalJsonError",
    "CanonicalStringError",
    "canonical_json_bytes",
    "seed_fingerprint_bytes",
]

_SURROGATE = re.compile("[\ud800-\udfff]")
# A lone surrogate written by the ASCII encoder is always `\ud8xx` to `\udfxx`. A literal
# backslash-u in data is written `\\u`, which also matches here, so a hit only asks for the walk.
_ESCAPED_SURROGATE = re.compile(r"\\u[dD][89a-fA-F][0-9a-fA-F]{2}")


class CanonicalJsonError(DecoyError, ValueError):
    """A value cannot be written in canonical JSON. Value-free: it names a type at most.

    It is a ``ValueError`` so the records codec's ``except ValueError`` arms turn it into the
    coded ``record_invalid``.
    """

    code = reasons.config.CONFIG_VALUE_NOT_JSON

    def __init__(self, message: str, type_name: str | None = None) -> None:
        super().__init__(message, type_name)
        self.message = message
        self.type_name = type_name

    def __str__(self) -> str:
        return self.message


class CanonicalStringError(CanonicalJsonError):
    """A string or object key holds an unpaired surrogate, so it has no UTF-8 form."""

    code = reasons.config.CANONICAL_JSON_INVALID_STRING

    def __init__(self) -> None:
        super().__init__(_summary(self.code))


def _summary(code: str) -> str:
    return reasons.REASONS[("config", code)].summary


def _has_surrogate(value: Any) -> bool:
    """Whether any string value or object key holds a code point in the surrogate range."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _SURROGATE.search(item):
                return True
        elif isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str) and _SURROGATE.search(key):
                    return True
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


def _encode(value: object, *, ensure_ascii: bool) -> bytes:
    refused: list[str] = []

    def default(item: object) -> str:
        if isinstance(item, dt.datetime):
            if item.utcoffset() is not None:
                return item.isoformat()
        elif isinstance(item, dt.date):
            return item.isoformat()
        refused.append(type(item).__name__)
        raise TypeError

    text: str | None = None
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=ensure_ascii,
            default=default,
        )
    except (TypeError, ValueError, RecursionError):
        pass
    if text is None:
        if refused:
            raise CanonicalJsonError(
                f"Canonical JSON cannot encode a value of type {refused[0]}.", refused[0]
            )
        raise CanonicalJsonError(
            "Canonical JSON cannot encode a non-finite number, a circular value "
            "or object keys of mixed types."
        )
    hint = _ESCAPED_SURROGATE if ensure_ascii else _SURROGATE
    if hint.search(text) and _has_surrogate(value):
        raise CanonicalStringError()
    return text.encode("utf-8")


def canonical_json_bytes(value: object) -> bytes:
    """The digest form: sorted, compact, UTF-8 text written as-is. Raises ``CanonicalJsonError``."""
    return _encode(value, ensure_ascii=False)


def seed_fingerprint_bytes(value: object) -> bytes:
    """The frozen seed form: as ``canonical_json_bytes`` but non-ASCII text is ``\\uXXXX`` escaped.

    Callable only from ``strategy_config_fingerprint`` and the pool ``_config_hash``; a sentry
    holds that list. Changing a byte this writes is a seed protocol change.
    """
    return _encode(value, ensure_ascii=True)
