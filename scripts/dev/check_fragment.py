"""PR check: a change to `src/` carries one changelog fragment.

    python scripts/dev/check_fragment.py --base origin/main [--repo PATH]

Compares HEAD with its merge base with `--base`. Two rules:

1. Every fragment the PR adds or changes under `changelog.d/` is a `<slug>.md` file that
   `stamp_release.parse_fragment` accepts. The grammar lives there and nowhere else.
2. A PR that changes `src/` adds at least one fragment, unless every non-merge commit in
   `merge-base..HEAD` is a `test`, `docs`, `ci`, `chore` or `refactor` Conventional Commit
   (a `!` breaking marker or a subject that does not parse is never exempt).

Exit codes: 0 ok; 1 a rule failed; 2 precondition (unknown base, not a repository). Messages carry
paths, short commit ids and rule names, never commit text or file content.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Scripts run by file path get their own directory on sys.path, not the repo root.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.release.stamp_release import (  # noqa: E402
    FRAGMENT_DIR,
    README,
    FragmentError,
    is_fragment_name,
    parse_fragment,
)

EXEMPT_TYPES = frozenset({"test", "docs", "ci", "chore", "refactor"})
_SUBJECT = re.compile(r"(?P<type>[a-z]+)(?:\([^()\s]*\))?(?P<bang>!)?: \S.*")


class PreconditionError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed git executable, literal arguments
        ["git", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise PreconditionError(f"git {args[0]} failed")
    return proc.stdout


def exempt_subject(subject: str) -> bool:
    m = _SUBJECT.fullmatch(subject)
    return m is not None and m.group("bang") is None and m.group("type") in EXEMPT_TYPES


def _changes(repo: Path, merge_base: str) -> list[tuple[str, str]]:
    """(status letter, path) per changed file; renames are split into delete plus add."""
    out = _git(repo, "diff", "--name-status", "--no-renames", "-z", merge_base, "HEAD")
    fields = out.split("\0")
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def check(repo: Path, base: str) -> list[str]:
    merge_base = _git(repo, "merge-base", base, "HEAD").strip()
    changes = _changes(repo, merge_base)
    prefix = f"{FRAGMENT_DIR}/"
    problems: list[str] = []
    added = 0
    for status, path in changes:
        if not path.startswith(prefix) or status == "D":
            continue
        name = path[len(prefix) :]
        if name == README:
            continue
        if not is_fragment_name(name):
            problems.append(f"{path}: not a '<slug>.md' fragment file name")
            continue
        try:
            parse_fragment((repo / path).read_text(encoding="utf-8"))
        except FragmentError as err:
            problems.append(f"{path}: {err}")
            continue
        added += status == "A"
    if added or not any(path.startswith("src/") for _, path in changes):
        return problems
    log = _git(repo, "log", "--no-merges", "--format=%h %s", f"{merge_base}..HEAD")
    blocking = [
        line.split(" ", 1)[0]
        for line in log.splitlines()
        if not exempt_subject(line.split(" ", 1)[1])
    ]
    if blocking:
        problems.append(
            f"src/ changed and no changelog fragment was added (commits needing one: {', '.join(blocking)})"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--base", required=True)
    ap.add_argument("--repo", type=Path, default=_ROOT)
    args = ap.parse_args(argv)
    try:
        problems = check(args.repo, args.base)
    except PreconditionError as err:
        print(f"check_fragment: {err}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"check_fragment: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
