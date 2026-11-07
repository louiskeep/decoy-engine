"""Print the EVIDENCE block for scripts/dev/gates.sh from its output directory.

gates.sh writes, per run label (`branch`, `baseline`): `<label>.steps` with one
tab-separated `name rc cmd` line per step, `<label>-<name>.log`, and for pytest
steps `<label>-<name>.xml` (JUnit, family xunit1 so testcases carry `file`). Counts
and failing ids both come from the JUnit file; log text is never parsed for ids. Only
pytest's final totals line is read from the log, to cross-check the JUnit.
It also owns the stored failing-id baseline (`--write-baseline`, the compare against a
stored file) and its run mode (`run_mode`, `check_stored`).
Stdlib only, so it runs in any venv."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

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


def collected(out: Path, label: str) -> int:
    """Test cases the `tests` step ran or skipped, from its JUnit file (deselected are not in it)."""
    return sum(int(s.get("tests", 0)) for s in _suites(out, label))


def _suites(out: Path, label: str) -> list[ET.Element]:
    xml = out / f"{label}-tests.xml"
    if not xml.exists():
        return []
    root = ET.parse(xml).getroot()  # noqa: S314 -- pytest JUnit output this run wrote
    return [root] if root.tag == "testsuite" else list(root.iter("testsuite"))


def skipped(out: Path, label: str) -> int:
    return sum(int(s.get("skipped", 0)) for s in _suites(out, label))


def file_seconds(out: Path, label: str) -> dict[str, float]:
    """Seconds per collected test file, summed from the JUnit `time` of every case."""
    total: dict[str, float] = {}
    for suite in _suites(out, label):
        for case in suite.iter("testcase"):
            if case.get("file"):
                path, _below = _locate(case)
                total[path] = total.get(path, 0.0) + float(case.get("time", 0) or 0)
    return {k: round(total[k], 3) for k in sorted(total)}


# Where classnames are resolved to files. A test inherited from a base class carries the file
# that defines it in `file` but the collected module in `classname`, and only the collected
# module is a node id pytest can re-run.
TEST_ROOT = Path(__file__).resolve().parents[2]


def _locate(case: ET.Element) -> tuple[str, list[str]]:
    """The collected test file and the class names below it, for a testcase that names a file."""
    file, cls = case.get("file") or "", case.get("classname", "")
    parts = cls.split(".") if cls else []
    for n in range(len(parts), 0, -1):
        rel = "/".join(parts[:n]) + ".py"
        if (TEST_ROOT / rel).is_file():
            return rel, parts[n:]
    module = file.removesuffix(".py").replace("/", ".")
    below = cls[len(module) :].strip(".") if cls.startswith(module) else cls
    return file, [c for c in below.split(".") if c]


def _case_id(case: ET.Element) -> str:
    """pytest node id from an xunit1 testcase: collected file, classes below it, then name."""
    file, cls, name = case.get("file"), case.get("classname", ""), case.get("name", "")
    if not file:
        return f"{cls}::{name}"
    if not cls:  # a collection error is reported against the file itself
        return file
    path, classes = _locate(case)
    return "::".join([path, *classes, name])


def _xml_failing_ids(xml: Path) -> list[str]:
    root = ET.parse(xml).getroot()  # noqa: S314 -- pytest JUnit output this run wrote
    ids: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        cid = _case_id(case)
        if cid not in ids:
            ids.append(cid)
    return ids


def junit_failing_ids(out: Path, label: str) -> list[str]:
    xml = out / f"{label}-tests.xml"
    return _xml_failing_ids(xml) if xml.exists() else []


def junit_failure_elements(out: Path, label: str) -> int:
    """Failure and error outcomes in the tests JUnit, one per element (a test can have two)."""
    return sum(
        len(case.findall("failure")) + len(case.findall("error"))
        for suite in _suites(out, label)
        for case in suite.iter("testcase")
    )


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TOTALS = re.compile(r"\b(\d+) (?:failed|errors?)\b")
_TOTALS_LINE = re.compile(
    r"^=*\s*(?:\d+ [a-z]+(?: [a-z]+)*(?:, \d+ [a-z]+(?: [a-z]+)*)*|no tests ran) in [\d.]+s(?: \([^)]*\))?\s*=*$"
)


def pytest_totals_failed(out: Path, label: str) -> int | None:
    """`N failed` plus `M error(s)` from pytest's final totals line, None if there is none."""
    log = out / f"{label}-tests.log"
    if not log.exists():
        return None
    for line in reversed(_ANSI.sub("", log.read_text(errors="replace")).splitlines()):
        if _TOTALS_LINE.match(line.strip()):
            return sum(int(n) for n in _TOTALS.findall(line))
    return None


def failing_ids(out: Path, label: str, only: str | None = None) -> list[str]:
    ids: list[str] = []
    for name, _rc, _cmd in _steps(out, label):
        xml = out / f"{label}-{name}.xml"
        if (only is not None and name != only) or not xml.exists():
            continue
        ids += [t for t in _xml_failing_ids(xml) if t not in ids]
    return ids


_PASSING = (0, -1, -2)
# On the branch side a step that did not run (-2) proved nothing, so it is never a pass.
_BRANCH_PASSING = (0, -1)
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
    """Every failing or erroring JUnit testcase, counted without deduplication.

    Complete only when at least one case failed and the count matches the suites' own failures
    plus errors totals, so a partial or truncated report is never read as shared with main."""
    xml = out / f"{label}-{name}.xml"
    keys: Counter[tuple[str, str, str]] = Counter()
    if not xml.exists():
        return keys, False
    root = ET.parse(xml).getroot()  # noqa: S314 -- pytest JUnit output this run wrote
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            keys[(_case_id(case), "", "")] += 1
    declared = sum(
        int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in root.iter("testsuite")
    )
    return keys, bool(keys) and declared == sum(keys.values())


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
        if rc in _BRANCH_PASSING:
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


# How a baseline was produced. A compare is only meaningful against a baseline written the same
# way, so these fields travel with the file and `check_stored` refuses any difference. `DIST` and
# the workers value are fixed here until gates.sh can run the suite in parallel.
MODE_KEYS = ("workers", "dist", "platform", "order")
DIST = "no"
# The stored file's layout version; bump it when a field changes meaning or is added.
FORMAT_VERSION = 1


def order_label(order_seconds: dict[str, float]) -> str:
    """`collection` for an empty map, else a hash of the canonical map the order was built from."""
    if not order_seconds:
        return "collection"
    canon = json.dumps(order_seconds, sort_keys=True, separators=(",", ":"))
    return "file-seconds:" + hashlib.sha256(canon.encode()).hexdigest()


def run_mode(workers: int, order_seconds: dict[str, float]) -> dict[str, object]:
    return {
        "workers": workers,
        "dist": DIST,
        "platform": f"{sys.platform}-{platform.machine()}",
        "order": order_label(order_seconds),
    }


class BaselineError(ValueError):
    """A stored baseline that may not be compared; the message says why and what to do."""


@dataclass(frozen=True)
class Baseline:
    """The one model of a stored baseline file: every reader loads it, the writer serializes it.

    `load_baseline` is the only place that reads the JSON, so no consumer sees an unvalidated
    field (a JSON `true` is not a count, a mode field is not a guess)."""

    repo: str
    main_sha: str
    head_sha: str
    collected: int
    skipped: int
    failing: tuple[str, ...]
    workers: int
    dist: str
    platform: str
    order: str
    order_seconds: dict[str, float]
    file_seconds: dict[str, float]

    def mode(self) -> dict[str, object]:
        return {k: getattr(self, k) for k in MODE_KEYS}

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["failing"] = sorted(self.failing)
        return {"format_version": FORMAT_VERSION, **data}


_FIELDS = tuple(Baseline.__dataclass_fields__)


def _is_count(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_seconds_map(v: object) -> bool:
    return isinstance(v, dict) and all(
        isinstance(k, str)
        and isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
        and x >= 0
        for k, x in v.items()
    )


def _parse_baseline(raw: object) -> Baseline:
    if not isinstance(raw, dict):
        raise BaselineError("the stored baseline is not a JSON object; rewrite the baseline")
    version = raw.get("format_version")
    if not _is_count(version) or version != FORMAT_VERSION:
        raise BaselineError(
            f"the stored baseline has format_version {version!r}, this gates.sh reads "
            f"format_version {FORMAT_VERSION}; rewrite the baseline"
        )
    missing = [k for k in _FIELDS if k not in raw]
    if missing:
        raise BaselineError(
            f"the stored baseline has no {', '.join(missing)}; rewrite the baseline"
        )
    extra = sorted(set(raw) - set(_FIELDS) - {"format_version"})
    if extra:
        raise BaselineError(
            f"the stored baseline has unknown field(s) {', '.join(extra)}; rewrite the baseline"
        )
    checks = {
        "repo": isinstance(raw["repo"], str),
        "main_sha": isinstance(raw["main_sha"], str) and bool(raw["main_sha"]),
        "head_sha": isinstance(raw["head_sha"], str),
        "collected": _is_count(raw["collected"]),
        "skipped": _is_count(raw["skipped"]),
        "failing": isinstance(raw["failing"], list)
        and all(isinstance(t, str) for t in raw["failing"]),
        "workers": _is_count(raw["workers"]) and raw["workers"] >= 1,
        "dist": isinstance(raw["dist"], str),
        "platform": isinstance(raw["platform"], str),
        "order": isinstance(raw["order"], str),
        "order_seconds": _is_seconds_map(raw["order_seconds"]),
        "file_seconds": _is_seconds_map(raw["file_seconds"]),
    }
    bad = [k for k, ok in checks.items() if not ok]
    if bad:
        raise BaselineError(
            f"the stored baseline's {', '.join(bad)} has the wrong type; rewrite the baseline"
        )
    fields = {k: raw[k] for k in _FIELDS}
    fields["failing"] = tuple(raw["failing"])
    return Baseline(**fields)


def load_baseline(path: Path) -> Baseline:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise BaselineError(
            "the stored baseline is not readable JSON; rewrite the baseline"
        ) from exc
    return _parse_baseline(raw)


def stored_failing_under(path: Path, prefixes: list[str]) -> bool:
    """True when a stored failing id starts with any prefix (a test file path or a directory)."""
    return any(t.startswith(p) for t in load_baseline(path).failing for p in prefixes)


def check_stored(path: Path, repo: str, workers: int) -> list[str]:
    """Reasons this run may not be compared with the stored baseline (empty when it may)."""
    try:
        stored = load_baseline(path)
    except BaselineError as exc:
        return [str(exc)]
    if stored.repo != repo:
        return [f"the stored baseline is for repo {stored.repo!r}, this run is {repo!r}"]
    if stored.order != order_label(stored.order_seconds):
        return [
            "the stored baseline's order does not match its order_seconds; rewrite the baseline"
        ]
    theirs = stored.mode()
    # This gates.sh runs in collection order, whatever order map the baseline was built with, so
    # a baseline ordered by a map differs until gates.sh can order its run from that map.
    mine = run_mode(workers, {})
    if theirs == mine:
        return []
    diff = ", ".join(k for k in MODE_KEYS if theirs[k] != mine[k])
    return [
        f"run mode differs in {diff}: baseline {json.dumps(theirs)}, this run {json.dumps(mine)}; "
        "rewrite the baseline in this mode"
    ]


def _stored_diff(out: Path, whole: bool) -> None:
    """Diff the branch's failing ids against the stored baseline; new ids go to `new-failing`."""
    stored = load_baseline(out / "stored-baseline.json")
    base = list(stored.failing)
    now = junit_failing_ids(out, "branch")
    new = [t for t in now if t not in base]
    print("-- STORED BASELINE DIFF --")
    print(f"  baseline: {stored.repo} main_sha={stored.main_sha}")
    for tid in new:
        print(f"  NEW-FAILING   {tid}")
    short_file = out / "shortfall"
    short_file.write_text("")
    if whole:
        fixed = [t for t in base if t not in now]
        for tid in fixed:
            print(f"  NOW-PASSING   {tid}")
        delta = collected(out, "branch") - stored.collected
        print(
            f"  collected: baseline={stored.collected} branch={collected(out, 'branch')} delta={delta:+d}"
        )
        print(
            f"  (new={len(new)} now-passing={len(fixed)} known-still-failing={len(now) - len(new)})"
        )
        if delta < 0:  # gates.sh decides whether the branch's own deletions explain it
            short_file.write_text(f"{-delta}\n")
    else:
        print("  selection run: now-passing and collected delta are not meaningful, skipped")
        print(f"  (new={len(new)})")
    skip_file = out / "skip-increase"
    skip_file.write_text("")
    now_skipped = skipped(out, "branch")
    if now_skipped > stored.skipped:
        print(
            f"  WARNING: branch skipped {now_skipped} tests, the baseline skipped {stored.skipped}"
        )
        skip_file.write_text(f"{now_skipped}\n")
    (out / "new-failing").write_text("".join(f"{t}\n" for t in new))


def write_baseline(
    out: Path, dest: Path, repo: str, main_sha: str, head_sha: str, workers: int
) -> None:
    mode = run_mode(workers, {})
    data = Baseline(
        repo=repo,
        main_sha=main_sha,
        head_sha=head_sha,
        collected=collected(out, "branch"),
        skipped=skipped(out, "branch"),
        failing=tuple(junit_failing_ids(out, "branch")),
        workers=workers,
        dist=str(mode["dist"]),
        platform=str(mode["platform"]),
        order=str(mode["order"]),
        order_seconds={},
        file_seconds=file_seconds(out, "branch"),
    )
    dest.write_text(json.dumps(data.to_json(), indent=2) + "\n")
    # stderr: stdout must still end with the EVIDENCE closing rule.
    print(
        f"gates: wrote baseline {dest} ({len(data.failing)} failing, collected {data.collected})",
        file=sys.stderr,
    )


def main(out: Path, whole: bool = False) -> int:
    meta = (out / "meta").read_text()
    labels = ["branch"] + (["baseline"] if (out / "baseline.steps").exists() else [])
    stored = (out / "stored-baseline.json").exists()
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
    if stored:
        _stored_diff(out, whole)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--verdict"]:
        print("\n".join(branch_only_steps(Path(sys.argv[2]))))
        sys.exit(0)
    if sys.argv[1:2] == ["--check-stored"]:
        # --check-stored FILE REPO WORKERS: exit 2 and say why when the file may not be compared.
        # On success it prints the stored main_sha, so gates.sh parses no JSON itself.
        reasons = check_stored(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
        for reason in reasons:
            print(f"gates: {reason}", file=sys.stderr)
        if not reasons:
            print(load_baseline(Path(sys.argv[2])).main_sha)
        sys.exit(2 if reasons else 0)
    if sys.argv[1:2] == ["--suite-collected"]:
        # --suite-collected OUT: cases in the branch tests JUnit; 0 when missing or unreadable.
        try:
            print(collected(Path(sys.argv[2]), "branch"))
        except (ET.ParseError, ValueError):
            print(0)
        sys.exit(0)
    if sys.argv[1:2] == ["--import-origin"]:
        # --import-origin PKG: where PKG imports from, or the import error; keeps gates.sh free of -c.
        import importlib

        try:
            print(importlib.import_module(sys.argv[2]).__file__)
        except Exception as exc:  # diagnostics line for the meta file
            print(f"{type(exc).__name__}: {exc}")
        sys.exit(0)
    if sys.argv[1:2] == ["--stored-failing-under"]:
        # --stored-failing-under FILE PREFIX...: exit 0 when a stored failing id starts with one.
        sys.exit(0 if stored_failing_under(Path(sys.argv[2]), sys.argv[3:]) else 1)
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--whole-suite", action="store_true")
    ap.add_argument("--write-baseline", type=Path)
    ap.add_argument("--repo", default="")
    ap.add_argument("--main-sha", default="")
    ap.add_argument("--head-sha", default="")
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()
    rc = main(args.out, args.whole_suite)
    ran_tests = (args.out / "branch-tests.xml").exists()
    if ran_tests and (args.write_baseline or (args.out / "stored-baseline.json").exists()):
        action = "store" if args.write_baseline else "compare against"
        reported = pytest_totals_failed(args.out, "branch")
        in_junit = junit_failure_elements(args.out, "branch")
        if reported is None or reported != in_junit:
            seen = "no totals line" if reported is None else f"{reported} failed/error"
            print(
                f"gates: pytest printed {seen} but the JUnit has {in_junit}; refusing to {action} the baseline",
                file=sys.stderr,
            )
            sys.exit(4)
    if args.write_baseline:
        write_baseline(
            args.out, args.write_baseline, args.repo, args.main_sha, args.head_sha, args.workers
        )
    sys.exit(rc)
