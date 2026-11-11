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

The census half of the module is shared by the size and fan-out sentries: ``loc`` and
``direct_modules`` measure, and ``ratchet_verdict`` is the one policy that turns a measurement
and its recorded allowlist value into a failure message or ``None``. ``ignore_keys`` reads the
ignore lines of an import-linter config. The CLI vendors this file byte for byte (see
``test_vendored_files.py``), so it imports nothing from this repo.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
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


def loc(py_file: Path) -> int:
    """Newline count, matching the `wc -l` convention the size cap is stated in
    (a final line without a trailing newline is not counted, exactly as wc -l)."""
    return py_file.read_text(encoding="utf-8").count("\n")


def direct_modules(directory: Path) -> int:
    """Count the ``*.py`` files directly in ``directory``; ``__init__.py`` and sub-packages do not count."""
    return sum(1 for p in directory.glob("*.py") if p.name != "__init__.py")


@dataclass(frozen=True)
class Terms:
    """The words a census verdict uses, so each sentry's messages name its own noun."""

    unit: str  # what is counted: "LOC", "modules"
    what: str  # the thing measured: "module", "package"
    table: str  # the allowlist constant: "ALLOWLIST"
    record: str  # what the allowlist is called in prose: "census", "freeze"
    fix: str  # imperative that removes the excess: "Decompose"
    hard: str = "hard max"  # name of the limit


SIZE_TERMS = Terms("LOC", "module", "ALLOWLIST", "census", "Decompose")
FANOUT_TERMS = Terms(
    "modules",
    "package",
    "FANOUT_ALLOWLIST",
    "freeze",
    "Move modules into a named sub-package",
    "limit",
)


def ratchet_verdict(
    rel: str,
    measured: int,
    recorded: int | None,
    *,
    goal: int,
    limit: int,
    margin: int,
    terms: Terms,
) -> str | None:
    """Pure policy decision: a failure message, or None to pass.

    ``recorded`` is the allowlist value (None when unrecorded). A value above ``limit`` is
    legacy: it must equal the measurement and may only shrink. A value between ``goal`` and
    ``limit`` is a ceiling with at most ``2 * margin`` of stale slack. Nothing unrecorded may
    pass ``limit``.
    """
    t = terms
    if recorded is None:
        if measured > limit:
            return (
                f"{rel}: {measured} {t.unit}, over the {limit}-{t.unit} {t.hard}. {t.fix} to "
                f"<= {limit}; a new {t.what} cannot be grandfathered above {limit}."
            )
        if measured > goal:
            return (
                f"{rel}: {measured} {t.unit}, over the {goal}-{t.unit} goal. {t.fix} to "
                f"<= {goal}, or (if it is genuinely dense) add it to {t.table} at {measured} "
                f"in this same PR for review -- allowed up to {limit}."
            )
        return None
    if recorded > limit:
        if measured == recorded:
            return None
        if measured < recorded:
            return (
                f"{rel}: shrank to {measured} {t.unit} ({t.record} records {recorded}). "
                f"Ratchet the {t.record} down to {measured} in this PR (or delete the entry "
                f"if <= {goal})."
            )
        return (
            f"{rel}: legacy over-max {t.what} grew to {measured} {t.unit} ({t.record} "
            f"{recorded}). A {t.what} over {limit} may only shrink -- {t.fix.lower()}, "
            f"never raise a legacy ceiling."
        )
    if measured > limit:
        return (
            f"{rel}: grew to {measured} {t.unit}, over the {limit}-{t.unit} {t.hard}. "
            f"{t.fix} to <= {limit}; a dense entry's ceiling may not cross {limit}."
        )
    if measured > recorded:
        return (
            f"{rel}: grew to {measured} {t.unit} (ceiling {recorded}). Allowed (<= {limit}); "
            f"raise the ceiling by at most the lines this change added (never above "
            f"{limit}) and list the moved pin in the report."
        )
    if recorded - measured > 2 * margin:
        return (
            f"{rel}: {measured} {t.unit} is {recorded - measured} under its ceiling "
            f"{recorded} (more than {2 * margin}). Ratchet the {t.record} down to "
            f"{min(limit, measured + margin)} in this PR (or delete the entry if <= {goal})."
        )
    return None


def ignore_keys(config_text: str) -> set[str]:
    """Return the ``importer -> imported`` ignore lines of an import-linter INI config."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read_string(config_text)
    keys: set[str] = set()
    for name in parser.sections():
        for line in parser.get(name, "ignore_imports", fallback="").splitlines():
            parts = [part.strip() for part in line.split("->")]
            if len(parts) == 2 and all(parts):
                keys.add(" -> ".join(parts))
    return keys
