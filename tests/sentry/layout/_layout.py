"""Shared helpers for the layout sentries.

``map_paths`` reads the path cells of a markdown table and reports the concrete paths
that do not exist. The same function checks the CODEMAP "Where Things Go" section
(base = repo root) and the in-package ``_MAP.md`` (base = ``src/decoy_engine``), so
there is one grammar for both.

Grammar for a checked code span:

* a span with neither ``/`` nor ``.py`` is a symbol (``SCALAR_HANDLERS``) and is skipped;
* a segment holding a ``<...>`` placeholder or ``*`` ends validation at the existing
  parent before that segment;
* a span followed at once by ``(target)`` is a future path: only its parent must exist;
* every other span is a concrete path and must exist below ``base``.

Messages carry paths only, never file contents.
"""

from __future__ import annotations

import re
from pathlib import Path

_SPAN = re.compile(r"`([^`]+)`( \(target\))?")
_PLACEHOLDER = re.compile(r"<[^>]*>|\*")


def section(markdown: str, heading: str) -> str:
    """Return the text under ``heading`` up to the next heading of the same or higher level."""
    lines = markdown.splitlines()
    level = len(heading) - len(heading.lstrip("#"))
    out: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == heading.strip():
            inside = True
            continue
        if inside and line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            break
        if inside:
            out.append(line)
    return "\n".join(out)


def _is_path(span: str) -> bool:
    return "/" in span or ".py" in span


def _required_path(span: str, is_target: bool) -> str:
    """Return the repo-relative path that must exist for ``span`` to be true."""
    parts = [p for p in span.split("/") if p not in ("", ".")]
    for i, part in enumerate(parts):
        if _PLACEHOLDER.search(part):
            return "/".join(parts[:i])
    if is_target:
        return "/".join(parts[:-1])
    return "/".join(parts)


def map_paths(markdown: str, base: Path, columns: tuple[int, ...]) -> list[str]:
    """List the concrete paths named in ``columns`` of the table rows that do not exist.

    ``columns`` are zero-based cell indexes. Rows are lines starting with ``|``; the
    separator row and header contain no code spans and so name no paths. The result is
    ``"<span> -> <path that is missing>"`` entries, empty when the map is true.
    """
    missing: list[str] = []
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        cells = line.strip().strip("|").split("|")
        for col in columns:
            if col >= len(cells):
                continue
            for match in _SPAN.finditer(cells[col]):
                span, target = match.group(1), bool(match.group(2))
                if not _is_path(span):
                    continue
                needed = _required_path(span, target)
                if not (base / needed).exists():
                    missing.append(f"{span} -> {needed or '.'}")
    return missing
