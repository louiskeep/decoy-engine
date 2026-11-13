"""The one parser for the `"NNMB"` memory-limit string the budget code emits and DuckDB reads."""

from __future__ import annotations

import re

_PATTERN = re.compile(r"\A(\d+)MB\Z")


def parse_megabytes(limit: str | None) -> int | None:
    """The count in a `"NNMB"` string, or None for any other shape (including None)."""
    match = None if limit is None else _PATTERN.fullmatch(limit)
    return None if match is None else int(match.group(1))


def require_megabytes(limit: str) -> int:
    """`parse_megabytes` for a limit that must be well formed: any other shape raises, so a
    malformed string is never passed on to DuckDB as if it were the smallest cap."""
    megabytes = parse_megabytes(limit)
    if megabytes is None:
        raise ValueError("memory_limit must look like '512MB'")
    return megabytes
