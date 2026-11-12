"""Walk from the workflows to the CI lanes: which pytest marker runs where, and on which event.

The rules (M1 to M6 markers, T1 to T4 triggers, R1 to R3 registries) are listed in
docs/plans/2026-11-11-ci-e2.md and seeded in test_ci_lanes.py. Pure functions over parsed data, so
a seeded case is a modified copy of the real tree and needs no files on disk.

Covered pytest spellings: `pytest`, `python -m pytest`, `uv run [opts] pytest` and `bash -c "..."`,
with `-m X`, `-mX`, the last `-m` winning, and `-o addopts=`. Deliberate evasion (PYTEST_ADDOPTS in
an env block, config-file indirection, command substitution) is out of scope (V2-LOG).
Shell parsing is the shared ci_scan._shell_tokens.commands; nothing here tokenizes shell.
"""

from __future__ import annotations

import ast
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from tests.sentry.ci_scan._shell_tokens import base, commands

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[3]
AUTOMATIC = {"push", "pull_request", "schedule"}
PR_EVENTS = {"pull_request", "pull_request_target"}
NOT_PR_IF = "github.event_name != 'pull_request'"
CONCURRENCY = {
    "group": "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
    "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
}
# pytest options that take a value, so the value is not read as a test path.
_VALUE_OPTS = {
    "-k", "-p", "-n", "-c", "-W", "--tb", "--junitxml", "--durations", "--ignore", "--ignore-glob",
    "--deselect", "--maxfail", "--rootdir", "--basetemp", "--timeout", "--cov",
}  # fmt: skip
_ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
_PYTHON = re.compile(r"python[0-9.]*")
_BAD_EXPR = (ast.BoolOp, ast.UnaryOp, ast.Name, ast.And, ast.Or, ast.Not, ast.Load)


class ExprError(ValueError):
    """A -m expression outside the `and`, `or`, `not`, parentheses and names grammar."""


@dataclass(frozen=True)
class Tree:
    """Everything the rules read, parsed: workflows by file name, the two registries, gates.sh."""

    workflows: dict[str, dict[Any, Any]]
    pyproject: dict[str, Any]
    gates: str
    unlaned: dict[str, Any]
    required: dict[str, Any]


class Lane(NamedTuple):
    workflow: str
    job: str
    events: frozenset[str]  # automatic events that start the job
    expr: str | None  # the -m expression; None selects every test
    whole_suite: bool
    bad: bool  # the expression is outside the grammar


def load(root: Path = ROOT) -> Tree:
    flows = {
        p.name: yaml.safe_load(p.read_text())
        for p in sorted((root / ".github/workflows").glob("*.yml"))
    }
    return Tree(
        flows,
        tomllib.loads((root / "pyproject.toml").read_text()),
        (root / "scripts/dev/gates.sh").read_text(),
        tomllib.loads((root / "ci/unlaned.toml").read_text()),
        tomllib.loads((root / "ci/required-jobs.toml").read_text()),
    )


def _on(doc: dict[Any, Any]) -> Any:
    return doc[True] if True in doc else doc.get("on")  # YAML 1.1 reads a bare `on:` key as True


def workflow_events(doc: dict[Any, Any]) -> set[str]:
    """The trigger names of a workflow: the scalar, list and mapping forms of `on`."""
    on = _on(doc)
    if isinstance(on, str):
        return {on}
    if isinstance(on, (list, dict)):
        return set(on)
    raise ValueError("workflow has no readable `on` trigger")


def job_events(events: set[str], job: dict[str, Any]) -> set[str]:
    """Events that start a job: all of the workflow's, minus pull_request for the exact not-PR
    guard, none for any other `if`."""
    if "if" not in job:
        return set(events)
    return events - {"pull_request"} if str(job["if"]).strip() == NOT_PR_IF else set()


def may_run_on_pr(events: set[str], job: dict[str, Any]) -> bool:
    return bool(events & PR_EVENTS) and str(job.get("if", "")).strip() != NOT_PR_IF


def _check_expr(expr: str) -> ast.expr:
    try:
        node = ast.parse(expr.strip(), mode="eval").body
    except SyntaxError:
        raise ExprError from None
    for sub in ast.walk(node):
        if not isinstance(sub, _BAD_EXPR):
            raise ExprError
        if isinstance(sub, ast.UnaryOp) and not isinstance(sub.op, ast.Not):
            raise ExprError
    return node


def selects(expr: str | None, marks: frozenset[str]) -> bool:
    """Whether a test carrying exactly `marks` is selected by `-m expr` (None selects all)."""
    if expr is None:
        return True

    def ev(n: ast.expr) -> bool:
        if isinstance(n, ast.Name):
            return n.id in marks
        if isinstance(n, ast.UnaryOp):
            return not ev(n.operand)
        assert isinstance(n, ast.BoolOp)
        return all(map(ev, n.values)) if isinstance(n.op, ast.And) else any(map(ev, n.values))

    return ev(_check_expr(expr))


def _ini(pyproject: dict[str, Any]) -> dict[str, Any]:
    return pyproject["tool"]["pytest"]["ini_options"]


def addopts_expr(pyproject: dict[str, Any]) -> str | None:
    args = shlex.split(_ini(pyproject).get("addopts", ""))
    return args[args.index("-m") + 1] if "-m" in args else None


def _pytest_args(cmd: list[str]) -> list[str] | None:
    """The arguments after the `pytest` command word, or None when `cmd` is not pytest."""
    lead = 0
    while lead < len(cmd) and _ENV_ASSIGN.fullmatch(cmd[lead]):
        lead += 1
    cmd = cmd[lead:]
    for i, tok in enumerate(cmd):
        if base(tok) != "pytest":
            continue
        prev = cmd[:i]
        via_python = len(prev) >= 2 and prev[-1] == "-m" and _PYTHON.fullmatch(base(prev[-2]))
        via_uv = bool(prev) and (prev[0] == "uvx" or (prev[0] == "uv" and "run" in prev))
        if not prev or via_python or via_uv:
            return cmd[i + 1 :]
    return None


def _read_args(args: list[str], default: str | None) -> tuple[str | None, bool, bool]:
    """(expression, whole_suite, bad) of one pytest command line; the last -m wins."""
    expr, has_m, bad, paths, i = default, False, False, [], 0
    while i < len(args):
        tok = args[i]
        if tok == "-m":
            expr, has_m, i = (args[i + 1] if i + 1 < len(args) else ""), True, i + 2
        elif tok.startswith("-m") and not tok.startswith("--"):
            expr, has_m, i = tok[2:], True, i + 1
        elif tok == "-o" and i + 1 < len(args):
            if args[i + 1].startswith("addopts=") and not has_m:
                rest = args[i + 1].removeprefix("addopts=").strip("\"'")
                expr, bad = (None, bad) if not rest else (rest, True)
            i += 2
        elif tok in _VALUE_OPTS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            paths.append(tok.split("::")[0].rstrip("/"))
            i += 1
    if expr is not None:
        try:
            _check_expr(expr)
        except ExprError:
            bad = True
    return expr, all(p == "tests" for p in paths), bad


def lanes(tree: Tree) -> list[Lane]:
    """Every pytest invocation in every job, with the automatic events that start the job."""
    found = []
    default = addopts_expr(tree.pyproject)
    for name, doc in tree.workflows.items():
        events = workflow_events(doc)
        for key, job in doc.get("jobs", {}).items():
            auto = frozenset(job_events(events, job) & AUTOMATIC)
            for step in job.get("steps", []):
                for cmd in commands(step.get("run", "")):
                    args = _pytest_args(cmd)
                    if args is not None:
                        found.append(Lane(name, key, auto, *_read_args(args, default)))
    return found


def _usable(lane: Lane, marks: frozenset[str]) -> bool:
    return bool(lane.events) and not lane.bad and selects(lane.expr, marks)


def marker_findings(tree: Tree) -> list[str]:
    default = addopts_expr(tree.pyproject)
    registered = [m.split(":", 1)[0].strip() for m in _ini(tree.pyproject)["markers"]]
    dedicated = {e.get("name") for e in tree.required.get("dedicated_marker", [])}
    unlaned = {e.get("name") for e in tree.unlaned.get("marker", [])}
    all_lanes = lanes(tree)
    out = [
        f"M4 {ln.workflow}:{ln.job}: unreadable pytest -m expression" for ln in all_lanes if ln.bad
    ]
    out += [
        f"M6 dedicated marker {n!r} is not registered" for n in sorted(dedicated - set(registered))
    ]
    out += [f"M3 unlaned marker {n!r} is not registered" for n in sorted(unlaned - set(registered))]
    for m in registered:
        mark = frozenset({m})
        whole = selects(default, mark) and m not in dedicated
        if whole:  # M1: a default marker needs a whole-suite run
            has = any(_usable(ln, mark) and ln.whole_suite for ln in all_lanes)
        else:  # M2 and M6: an opt-in or profile marker needs a run that picks it out
            has = any(_usable(ln, mark) and not selects(ln.expr, frozenset()) for ln in all_lanes)
        if not has and m not in unlaned:
            out.append(
                f"{'M1' if whole else 'M2'} marker {m!r} has no automatic lane and is not in ci/unlaned.toml"
            )
        if has and m in unlaned:
            out.append(f"M3 marker {m!r} has a lane but is still in ci/unlaned.toml")
    out += _agreement_findings(
        tree.gates, [ln for ln in all_lanes if ln[:2] == ("ci.yml", "regression-gate")]
    )
    return out


def _agreement_findings(gates: str, gate_lanes: list[Lane]) -> list[str]:
    """M5: gates.sh and the hosted full suite select the same tests."""
    pins = [ln for ln in gates.splitlines() if ln.startswith("PYTEST_ARGS=")]
    m = re.fullmatch(r"PYTEST_ARGS=\(-m '([^']+)'\)", pins[0]) if len(pins) == 1 else None
    if m is None:
        return ["M5 gates.sh has no single PYTEST_ARGS=(-m '<expr>') line"]
    if len(gate_lanes) != 1 or " ".join((gate_lanes[0].expr or "").split()) != " ".join(
        m.group(1).split()
    ):
        return ["M5 gates.sh PYTEST_ARGS and the ci.yml regression-gate -m expression differ"]
    return []


def _closed(doc: dict[str, Any], label: str, tables: dict[str, dict[str, type]]) -> list[str]:
    """R1: closed schema. Each table is a list of entries with exactly the named keys."""
    out = []
    if doc.get("schema") != 1:
        out.append(f"R1 {label}: schema must be 1")
    out += [f"R1 {label}: unknown table {t!r}" for t in sorted(set(doc) - set(tables) - {"schema"})]
    for table, fields in tables.items():
        seen = []
        for entry in doc.get(table, []):
            if set(entry) != set(fields):
                out.append(f"R1 {label}: [[{table}]] entry keys must be {sorted(fields)}")
                continue
            for key, kind in fields.items():
                val = entry[key]
                if not isinstance(val, kind) or not val or (kind is str and not val.strip()):
                    out.append(f"R1 {label}: [[{table}]] {key} must be a non-empty {kind.__name__}")
            ident = tuple(str(entry[k]) for k in fields if k != "reason")
            if ident in seen:
                out.append(f"R1 {label}: duplicate [[{table}]] entry {ident[0]}")
            seen.append(ident)
    return out


def registry_findings(tree: Tree) -> list[str]:
    out = _closed(
        tree.unlaned,
        "unlaned.toml",
        {"marker": {"name": str, "reason": str}, "workflow": {"file": str, "reason": str}},
    )
    out += _closed(
        tree.required,
        "required-jobs.toml",
        {
            "merge": {"workflow": str, "jobs": list},
            "merge_exempt": {"workflow": str, "job": str, "reason": str},
            "dedicated_marker": {"name": str, "reason": str},
        },
    )
    if out:
        return out  # the rules below assume well-formed entries
    merge = {e["workflow"]: list(e["jobs"]) for e in tree.required.get("merge", [])}
    exempt = {(e["workflow"], e["job"]) for e in tree.required.get("merge_exempt", [])}
    for wf, jobs in merge.items():
        doc = tree.workflows.get(wf)
        if doc is None:
            out.append(f"R2 {wf}: merge workflow does not exist")
            continue
        if "pull_request" not in workflow_events(doc):
            out.append(f"R2 {wf}: merge workflow has no pull_request trigger")
        for key in jobs:
            out += _merge_job_findings(wf, key, doc["jobs"].get(key), jobs)
    for wf, doc in tree.workflows.items():
        events = workflow_events(doc)
        for key, job in doc["jobs"].items():
            if (
                may_run_on_pr(events, job)
                and key not in merge.get(wf, [])
                and (wf, key) not in exempt
            ):
                out.append(
                    f"R3 {wf}:{key}: runs on pull requests but is in neither merge nor merge_exempt"
                )
    return out


def _merge_job_findings(
    wf: str, key: str, job: dict[str, Any] | None, tier: list[str]
) -> list[str]:
    if job is None:
        return [f"R2 {wf}:{key}: merge job does not exist"]
    needs = job.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    checks = {
        "has an `if` (a skipped job reports success)": "if" in job,
        "needs a job outside the merge tier": bool(set(needs) - set(tier)),
        "is self-hosted": "self-hosted" in str(job.get("runs-on", "")),
        "has a templated name": "${{" in str(job.get("name", "")),
    }
    return [f"R2 {wf}:{key}: merge job {why}" for why, bad in checks.items() if bad]


def trigger_findings(tree: Tree) -> list[str]:
    out = []
    unlaned = {e["file"] for e in tree.unlaned.get("workflow", []) if "file" in e}
    out += [
        f"T1 unlaned workflow {n!r} does not exist" for n in sorted(unlaned - set(tree.workflows))
    ]
    for name, doc in tree.workflows.items():
        events = workflow_events(doc)
        auto = bool(events & AUTOMATIC)
        if not auto and name not in unlaned:
            out.append(f"T1 {name}: no automatic trigger and not in ci/unlaned.toml")
        if auto and name in unlaned:
            out.append(f"T1 {name}: has an automatic trigger but is still in ci/unlaned.toml")
        on = _on(doc)
        for event, spec in on.items() if isinstance(on, dict) else []:
            if isinstance(spec, dict) and {"paths", "paths-ignore"} & set(spec):
                out.append(f"T2 {name}: path filter on {event}")
        out += [
            f"T3 {name}:{k}: no timeout-minutes"
            for k, j in doc["jobs"].items()
            if "timeout-minutes" not in j
        ]
        conc = doc.get("concurrency")
        if "pull_request" in events and conc != CONCURRENCY:
            out.append(f"T4 {name}: pull_request workflow lacks the shared concurrency block")
        if isinstance(conc, dict) and conc.get("cancel-in-progress") is False:
            out.append(f"T4 {name}: workflow-level cancel-in-progress is false")
    return out


def findings(tree: Tree) -> list[str]:
    return marker_findings(tree) + trigger_findings(tree) + registry_findings(tree)
