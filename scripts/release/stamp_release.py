"""Compile changelog fragments into CHANGELOG.md at release prep.

    python scripts/release/stamp_release.py --version X.Y.Z [--date YYYY-MM-DD] [--repo PATH]

Run on the release prep branch, in a full clone. It reads every `changelog.d/<slug>.md` (not the
README), orders them newest first by the commit that added each file, and writes one
`## [X.Y.Z] - DATE` section directly under the empty `## [Unreleased]` heading: the merged
"Breaking for consumers" list, then the fragments, then whatever `[Unreleased]` held before. It
deletes the fragments and adds the previous newest release to
`tests/fixtures/changelog_released_sections.json`.

`parse_fragment` is the one fragment grammar; `scripts/dev/check_fragment.py` calls it too.
Every write happens after every check, and CHANGELOG.md is replaced atomically. Messages carry
file names, line numbers and rule names, never file content.

Exit codes: 0 done; 1 refused (a malformed fragment, a shallow clone, an uncommitted fragment, a
bad version or date, a CHANGELOG without the expected headings).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

FRAGMENT_DIR = "changelog.d"
README = "README.md"
TYPES = ("Added", "Changed", "Fixed", "Deprecated", "Removed", "Security")
PIN_FILE = Path("tests") / "fixtures" / "changelog_released_sections.json"
BREAKING_HEADING = "### Breaking for consumers"

_BREAKING_LINE = re.compile(r"Breaking: (?P<text>\S.*)")
_HEADING = re.compile(rf"### (?:{'|'.join(TYPES)}) \(\S.*\)")
_ANY_H3 = re.compile(r"###(?!#)")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*\.md")
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?")
_RELEASE = re.compile(r"^## \[(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)\] - \d{4}-\d{2}-\d{2}$", re.M)
_ANY_H2 = re.compile(r"^## \[", re.M)


class FragmentError(ValueError):
    """A refusal. The message names files, lines and rules only."""


@dataclass(frozen=True)
class Fragment:
    breaking: str | None
    block: str


def parse_fragment(text: str) -> Fragment:
    """Strictly parse one fragment: optional `Breaking:` line, then exactly one heading and a body."""
    if "\r" in text:
        raise FragmentError("carriage return in fragment")
    lines = text.split("\n")
    i = 0
    breaking: str | None = None
    if lines[0].startswith("Breaking:"):
        m = _BREAKING_LINE.fullmatch(lines[0])
        if m is None:
            raise FragmentError("line 1: malformed Breaking line")
        breaking = m.group("text").rstrip()
        i = 1
    while i < len(lines) and lines[i] == "":
        i += 1
    if i >= len(lines) or _HEADING.fullmatch(lines[i]) is None:
        raise FragmentError(
            f"line {i + 1}: expected the '### <Type> (<title>, <slice>, <date>)' heading"
        )
    for n, line in enumerate(lines[i + 1 :], start=i + 2):
        if _ANY_H3.match(line):
            raise FragmentError(f"line {n}: a second '###' heading (use '####' for sub-headings)")
    block = "\n".join(lines[i:]).strip()
    if block == lines[i]:
        raise FragmentError(f"line {i + 1}: heading with no body")
    return Fragment(breaking, block)


def release_sections(text: str) -> list[tuple[str, str]]:
    """(version, section text from its heading up to the next release heading), newest first."""
    marks = list(_RELEASE.finditer(text))
    return [
        (m.group(1), text[m.start() : marks[i + 1].start() if i + 1 < len(marks) else len(text)])
        for i, m in enumerate(marks)
    ]


def section_digest(section: str) -> str:
    lines = [line.rstrip() for line in section.strip().splitlines()]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed git executable, literal arguments
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise FragmentError(f"git {args[0]} failed")
    return proc.stdout


def _fragment_files(root: Path) -> list[str]:
    folder = root / FRAGMENT_DIR
    names = []
    for p in sorted(folder.iterdir()):
        if p.name == README:
            continue
        if not p.is_file() or _SLUG.fullmatch(p.name) is None:
            raise FragmentError(f"{FRAGMENT_DIR}/{p.name}: not a '<slug>.md' fragment file")
        names.append(p.name)
    return names


def _newest_first(root: Path, names: list[str]) -> list[str]:
    """Fragment names ordered by the commit that added each file, newest first."""
    if _git(root, "rev-parse", "--is-shallow-repository").strip() != "false":
        raise FragmentError("shallow clone: fragment order needs the full history")
    log = _git(
        root,
        "log",
        "--topo-order",
        "--diff-filter=A",
        "--name-only",
        "--format=",
        "--",
        FRAGMENT_DIR,
    )
    seen: list[str] = []
    for line in log.splitlines():
        name = line.removeprefix(f"{FRAGMENT_DIR}/")
        if name in names and name not in seen:
            seen.append(name)
    missing = sorted(set(names) - set(seen))
    if missing:
        raise FragmentError(f"{FRAGMENT_DIR}/{missing[0]}: not added by any commit")
    return seen


def _split_breaking(body: str) -> tuple[str, list[str]]:
    """The body without its 'Breaking for consumers' lists, and those lists' text."""
    out: list[str] = []
    lists: list[str] = []
    parts = re.split(r"(?m)^(?=### )", body)
    for part in parts:
        if part.startswith(BREAKING_HEADING):
            lists.append(part[len(part.split("\n", 1)[0]) :].strip())
        else:
            out.append(part)
    return "".join(out).strip(), [x for x in lists if x]


def _compose(changelog: str, version: str, date: str, fragments: list[Fragment]) -> str:
    start = changelog.find("## [Unreleased]")
    if start == -1:
        raise FragmentError("CHANGELOG.md has no '## [Unreleased]' heading")
    after = start + len("## [Unreleased]")
    nxt = _ANY_H2.search(changelog, after)
    if nxt is None:
        raise FragmentError("CHANGELOG.md has no released section below [Unreleased]")
    old_body, old_lists = _split_breaking(changelog[after : nxt.start()])
    bullets = [f"- {f.breaking}" for f in fragments if f.breaking] + old_lists
    parts: list[str] = []
    if bullets:
        parts.append(BREAKING_HEADING + "\n\n" + "\n".join(bullets))
    parts += [f.block for f in fragments]
    if old_body:
        parts.append(old_body)
    section = f"## [{version}] - {date}\n\n" + "\n\n".join(parts) + "\n\n"
    return changelog[:after] + "\n\n" + section + changelog[nxt.start() :]


def _replace(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def compile_fragments(root: Path, version: str, date: str) -> None:
    """Fold the fragments and the old `[Unreleased]` body into a dated `version` section."""
    if _VERSION.fullmatch(version) is None:
        raise FragmentError("version must look like X.Y.Z")
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise FragmentError("date must be YYYY-MM-DD") from None
    names = _fragment_files(root)
    ordered = _newest_first(root, names)
    fragments: list[Fragment] = []
    for name in ordered:
        try:
            fragments.append(
                parse_fragment((root / FRAGMENT_DIR / name).read_text(encoding="utf-8"))
            )
        except FragmentError as err:
            raise FragmentError(f"{FRAGMENT_DIR}/{name}: {err}") from None

    changelog_path = root / "CHANGELOG.md"
    before = changelog_path.read_text(encoding="utf-8")
    if any(v == version for v, _ in release_sections(before)):
        raise FragmentError("CHANGELOG.md already has a section for this version")
    after = _compose(before, version, date, fragments)

    pin_path = root / PIN_FILE
    pins = json.loads(pin_path.read_text(encoding="utf-8"))
    sections = release_sections(after)
    prev_version, prev_section = sections[1]
    pins = {
        prev_version: section_digest(prev_section),
        **{k: v for k, v in pins.items() if k != prev_version},
    }

    _replace(changelog_path, after)
    _replace(pin_path, json.dumps(pins, indent=2) + "\n")
    for name in ordered:
        (root / FRAGMENT_DIR / name).unlink()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--version", required=True)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    args = ap.parse_args(argv)
    try:
        compile_fragments(args.repo, args.version, args.date)
    except FragmentError as err:
        print(f"stamp_release: refused: {err}", file=sys.stderr)
        return 1
    print(f"stamp_release: compiled changelog fragments into [{args.version}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
