"""Git and engine-import plumbing for scripts/dev/registry_renumber.py.

The extractor subprocess is the only place the tool imports the engine, and it trusts a
manifest only when `decoy_engine.reasons` came from the tree it was pointed at. Refusals carry
keys, identities, paths and line numbers, never file content or captured process output.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from scripts.release.reason_manifest_policy import SCHEMA_ROOTS, owned_schema_roots

REGISTRY_PY = "src/decoy_engine/reasons/_registry.py"
CONTRACT_PY = "tests/unit/progress/test_consumer_contract.py"
_EXTRACTOR = """
import json, runpy, sys
from decoy_engine import reasons
out = {"file": reasons.__file__, "manifest": reasons.manifest()}
if len(sys.argv) > 1:
    out["contract"] = runpy.run_path(sys.argv[1], run_name="renumber")["build_contract"]()
print(json.dumps(out))
"""
_TWICE = re.compile(r"reason (\w+\.\w+) is declared twice")
_NAME = re.compile(r"[\w.]+")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class Refusal(Exception):  # noqa: N818 - carries the exit code, not an error condition
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class Hunk(NamedTuple):
    path: str
    start: int  # first line on the new side
    removed: tuple[str, ...]
    added: tuple[tuple[int, str], ...]  # (new-side line number, text)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo), "-c", "core.quotepath=false", *args],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and res.returncode != 0:
        raise Refusal(2, "bad-ref")
    return res


def archive(repo: Path, ref: str, dest: Path, *paths: str) -> None:
    packed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo), "archive", "--format=tar", ref, *paths],  # noqa: S607
        capture_output=True,
    )
    if packed.returncode != 0:
        raise Refusal(2, "bad-ref")
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["tar", "-x", "-C", str(dest)],  # noqa: S607
        input=packed.stdout,
        check=True,
    )


def extract(tree: Path, contract: bool = False) -> dict:
    """The manifest (and the consumer contract) of the engine inside ``tree``."""
    env = {**os.environ, "PYTHONPATH": str(tree / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    args = [sys.executable, "-c", _EXTRACTOR, *([str(tree / CONTRACT_PY)] if contract else [])]
    with tempfile.TemporaryDirectory() as scratch:  # the engine may write logs under its cwd
        res = subprocess.run(args, capture_output=True, text=True, env=env, cwd=scratch)  # noqa: S603
    if res.returncode != 0:
        twice = _TWICE.search(res.stderr)
        if twice:
            raise Refusal(3, f"duplicate-identity {twice[1]}")
        last = (res.stderr.strip().splitlines() or [""])[-1].split(":")[0]
        raise Refusal(2, "import-failed " + (last if _NAME.fullmatch(last) else "unknown"))
    out = json.loads(res.stdout.strip().splitlines()[-1])
    if not Path(out["file"]).resolve().is_relative_to((tree / "src").resolve()):
        raise Refusal(2, "foreign-import")
    return out


def fork_point(repo: Path, main: str) -> str:
    """The commit the branch left main at: the first parent of its oldest first-parent commit."""
    chain = git(repo, "rev-list", "--first-parent", f"{main}..HEAD").stdout.split()
    return git(repo, "rev-parse", f"{chain[-1]}^").stdout.strip() if chain else main


def owned_roots(repo: Path, fork: str) -> frozenset[str]:
    """Schema roots the branch itself changed, by history (an inherited main change is not)."""
    changed: list[str] = []
    triples: list[tuple[str, str, str]] = []
    merges = git(repo, "rev-list", "--first-parent", "--merges", f"{fork}..HEAD").stdout.split()
    for root in SCHEMA_ROOTS:
        name = root.upper()
        pat = re.compile(rf"^{name} = (\d+)$", re.M)
        hit = git(repo, "log", "--first-parent", "--no-merges", f"-G^{name} =", "--format=%H",
                  f"{fork}..HEAD", "--", REGISTRY_PY)  # fmt: skip
        if hit.stdout.split():
            changed.append(root)
        for merge in merges:
            here, other = (
                pat.search(git(repo, "show", f"{rev}:{REGISTRY_PY}").stdout)
                for rev in (merge, f"{merge}^2")
            )
            triples.append((root, here[1] if here else "", other[1] if other else ""))
    return owned_schema_roots(changed, triples)


def hunks(repo: Path, main: str) -> Iterator[Hunk]:
    """Branch-added and main-side lines per hunk of `git diff -U0 main HEAD`."""
    diff = git(repo, "diff", "-U0", "--no-color", "--no-renames", "--no-textconv", main, "HEAD")
    path, cur = "", None
    removed: list[str] = []
    added: list[tuple[int, str]] = []
    start = line = 0

    def flush() -> Iterator[Hunk]:
        if cur is not None and (removed or added):
            yield Hunk(path, start, tuple(removed), tuple(added))

    for text in diff.stdout.split("\n"):
        if text.startswith("+++ "):
            yield from flush()
            path, cur = text[6:], None
            if text == "+++ /dev/null":
                path = ""
            removed, added = [], []
        elif m := _HUNK.match(text):
            yield from flush()
            cur, start, line = True, int(m[1]), int(m[1])
            removed, added = [], []
        elif cur and text.startswith("+"):
            added.append((line, text[1:]))
            line += 1
        elif cur and text.startswith("-") and not text.startswith("---"):
            removed.append(text[1:])
    yield from flush()
