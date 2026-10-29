"""Fixed-shape UTC timestamps: ``YYYY-MM-DDTHH:MM:SSZ`` and nothing else."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def parse_utc(text: str) -> datetime | None:
    """Return an aware UTC datetime, or None if ``text`` is not exactly the profile shape."""
    if not isinstance(text, str) or not _SHAPE.fullmatch(text):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
