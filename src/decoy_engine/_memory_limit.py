"""The one parser for the `"NNMB"` memory-limit string the budget code emits and DuckDB reads."""

from __future__ import annotations

import re

_PATTERN = re.compile(r"(\d+)MB")


def parse_megabytes(limit: str | None) -> int | None:
    """The count in a `"NNMB"` string, or None for any other shape (including None)."""
    match = None if limit is None else _PATTERN.fullmatch(limit)
    return None if match is None else int(match.group(1))
