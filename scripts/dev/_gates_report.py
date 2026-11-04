"""Print the EVIDENCE block for scripts/dev/gates.sh from its output directory.

gates.sh writes, per run label (`branch`, `baseline`): `<label>.steps` with one
tab-separated `name rc cmd` line per step, `<label>-<name>.log`, and for pytest
steps `<label>-<name>.xml` (JUnit). Counts come from the JUnit file; failing ids
come from pytest's `-rfE` short summary, which carries real node ids.
Stdlib only, so it runs in any venv."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

_SUMMARY = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$")
_FOUND = re.compile(r"^Found (\d+) errors?", re.MULTILINE)


def _steps(out: Path, label: str) -> list[tuple[str, int, str]]:
    path = out / f"{label}.steps"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        name, rc, cmd = line.split("\t", 2)
        rows.append((name, int(rc), cmd))
    return rows


def _counts(out: Path, label: str, name: str) -> str:
    xml = out / f"{label}-{name}.xml"
    if not xml.exists():
        # ruff and mypy both end with a "Found N error(s)" line when they fail.
        log = out / f"{label}-{name}.log"
        found = _FOUND.findall(log.read_text(errors="replace")) if log.exists() else []
        return f"errors={found[-1]}" if found else ""
    root = ET.parse(xml).getroot()  # noqa: S314 -- pytest JUnit output this run wrote
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = {
        k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")
    }
    passed = total["tests"] - total["failures"] - total["errors"] - total["skipped"]
    return f"passed={passed} failed={total['failures']} errors={total['errors']} skipped={total['skipped']}"


def failing_ids_for(out: Path, label: str, name: str) -> list[str]:
    log = out / f"{label}-{name}.log"
    if not (out / f"{label}-{name}.xml").exists() or not log.exists():
        return []
    ids: list[str] = []
    for line in log.read_text(errors="replace").splitlines():
        m = _SUMMARY.match(line)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def failing_ids(out: Path, label: str) -> list[str]:
    ids: list[str] = []
    for name, _rc, _cmd in _steps(out, label):
        ids += [i for i in failing_ids_for(out, label, name) if i not in ids]
    return ids


_PASSING = (0, -1, -2)
# Every step here exits 1 when it ran to completion and found problems (pytest, ruff, mypy, the
# ratchet check). Any other non-zero code is a crash, a kill or a timeout (124, 137, 139, pytest 2-5).
_NORMAL_FAILURE_RC = 1
_RUFF = re.compile(r"^(\S+?):\d+:\d+: ([A-Z]+\d+) (?:\[\*\] )?(.+)$")
_MYPY = re.compile(r"^(\S+?):\d+(?::\d+)?: error: (.+?)(?:  \[([\w-]+)\])?$")
_REFORMAT = re.compile(r"^Would reformat: (\S+)$")
_TOTAL = re.compile(r"^Found (\d+) errors?", re.MULTILINE)


def diagnostics(out: Path, label: str, name: str) -> tuple[Counter[tuple[str, str, str]], bool]:
    """Error lines of a non-pytest step as a multiset of (file, code, message), line numbers dropped.

    Line numbers are dropped so a moved line still matches, but nothing is deduplicated: a second
    identical error on another line counts twice. The flag is False when the "Found N errors"
    total disagrees with what was parsed, or nothing parsed: some failure is then invisible here."""
    log = out / f"{label}-{name}.log"
    keys: Counter[tuple[str, str, str]] = Counter()
    if not log.exists():
        return keys, False
    text = log.read_text(errors="replace")
    for line in text.splitlines():
        if m := _RUFF.match(line):
            keys[(m.group(1), m.group(2), m.group(3))] += 1
        elif m := _MYPY.match(line):
            keys[(m.group(1), m.group(3) or "", m.group(2))] += 1
        elif m := _REFORMAT.match(line):
            keys[(m.group(1), "format", "would reformat")] += 1
    totals = _TOTAL.findall(text)
    complete = bool(keys) and (not totals or int(totals[-1]) == sum(keys.values()))
    return keys, complete


def _pytest_failures(
    out: Path, label: str, name: str
) -> tuple[Counter[tuple[str, str, str]], bool]:
    ids = failing_ids_for(out, label, name)
    return Counter((i, "", "") for i in ids), bool(ids)


def branch_only_steps(out: Path) -> list[str]:
    """Steps that fail on the branch for a reason origin/main does not share.

    A branch step that fails is branch-only unless every one of its failures is identified and
    main has at least as many of each. So it is branch-only when: its exit code is not the normal
    failure code (crash, kill, timeout), whatever main did; it fails with nothing identifiable
    (no JUnit ids, no parseable diagnostics, or a total that disagrees with the parsed lines); or
    any identity occurs more often than on main. Failures are compared as multisets, so a
    duplicated identical error is a new failure. Only identical-or-fewer identified failures,
    with a normal failure code on the branch, are shared."""
    base = {name: rc for name, rc, _ in _steps(out, "baseline")}
    bad = []
    for name, rc, _cmd in _steps(out, "branch"):
        if rc in _PASSING:
            continue
        if rc != _NORMAL_FAILURE_RC:
            bad.append(name)
            continue
        if base.get(name, -1) in _PASSING:
            bad.append(name)  # main is green here, so every failure is the branch's
            continue
        if (out / f"branch-{name}.xml").exists():
            mine, complete = _pytest_failures(out, "branch", name)
            theirs, _ = _pytest_failures(out, "baseline", name)
        else:
            mine, complete = diagnostics(out, "branch", name)
            theirs, _ = diagnostics(out, "baseline", name)
        if not complete or mine - theirs:  # Counter subtraction keeps only positive excess
            bad.append(name)
    return bad


def _status(rc: int) -> str:
    return {0: "PASS", -1: "SKIP", -2: "NOT RUN"}.get(rc, f"FAIL(rc={rc})")


def _section(out: Path, label: str) -> None:
    print(f"-- {label} --")
    for name, rc, cmd in _steps(out, label):
        print(f"[{name}] {_status(rc)} {_counts(out, label, name)}".rstrip())
        print(f"    $ {cmd}")


def main(out: Path) -> int:
    meta = (out / "meta").read_text()
    labels = ["branch"] + (["baseline"] if (out / "baseline.steps").exists() else [])
    print("=" * 30 + " EVIDENCE " + "=" * 30)
    print(meta.rstrip())
    for label in labels:
        _section(out, label)
    branch = failing_ids(out, "branch")
    print("-- FAILING TEST IDS --")
    if len(labels) == 1:
        for tid in branch:
            print(f"  {tid}")
        print(f"  ({len(branch)} failing)")
    else:
        base = failing_ids(out, "baseline")
        for tid in branch + [t for t in base if t not in branch]:
            tag = (
                "BOTH"
                if tid in branch and tid in base
                else ("BRANCH-ONLY" if tid in branch else "BASELINE-ONLY")
            )
            print(f"  {tag:<14}{tid}")
        only = [t for t in branch if t not in base]
        print(f"  (branch={len(branch)} baseline={len(base)} branch-only={len(only)})")
        print("-- BRANCH-ONLY FAILING STEPS --")
        steps = branch_only_steps(out)
        print(f"  {', '.join(steps) if steps else '(none)'}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "--verdict":
        print("\n".join(branch_only_steps(Path(sys.argv[2]))))
        sys.exit(0)
    sys.exit(main(Path(sys.argv[1])))
