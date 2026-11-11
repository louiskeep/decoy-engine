"""The one failure type of the records package.

A record error carries a registry code and, for a payload that fails validation, the dotted
locations of the offending fields. It never carries a value, a path or the validator's message,
so it is safe to log.
"""

from __future__ import annotations

import re

from decoy_engine import reasons
from decoy_engine._error_base import DecoyError

_CODES = frozenset(
    {
        reasons.runtime.RECORD_INVALID,
        reasons.runtime.RECORD_CORRUPT,
        reasons.runtime.RECORD_KIND_RESERVED,
    }
)
_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_FIELDS = 8


def _safe_location(loc: tuple[object, ...]) -> str:
    # A key under a mapping field is caller data (a table name). Only identifier-shaped
    # segments are echoed; anything else is reduced to a placeholder.
    return ".".join(
        str(part) if isinstance(part, (str, int)) and _SEGMENT.fullmatch(str(part)) else "?"
        for part in loc
    )


class RecordError(DecoyError, ValueError):
    """A record cannot be built, decoded or exported. ``code`` is a ``runtime`` reason code."""

    def __init__(self, code: str, fields: tuple[tuple[object, ...], ...] = ()) -> None:
        if code not in _CODES:
            raise ValueError("unknown record error code")
        self.code = code
        self.fields = tuple(_safe_location(loc) for loc in fields[:_MAX_FIELDS])
        suffix = f" fields={','.join(self.fields)}" if self.fields else ""
        super().__init__(f"[{code}]{suffix}")
