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
from pathlib import Path

_SUMMARY = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$")


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
        return ""
    root = ET.parse(xml).getroot()  # noqa: S314 -- pytest JUnit output this run wrote
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = {
        k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")
    }
    passed = total["tests"] - total["failures"] - total["errors"] - total["skipped"]
    return f"passed={passed} failed={total['failures']} errors={total['errors']} skipped={total['skipped']}"


def failing_ids(out: Path, label: str) -> list[str]:
    ids: list[str] = []
    for name, _rc, _cmd in _steps(out, label):
        log = out / f"{label}-{name}.log"
        if not (out / f"{label}-{name}.xml").exists() or not log.exists():
            continue
        for line in log.read_text(errors="replace").splitlines():
            m = _SUMMARY.match(line)
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
    return ids


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
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
