"""Required-check verifier: the check names of the merge tier, and whether a head earned them.

    required_checks.py contexts [--ref REF]
    required_checks.py verify --sha H --tier merge [--allow-rerun WORKFLOW=REASON]
    required_checks.py verify --tier nightly

Run it with a Python that has PyYAML (the engine venv). GH_TOKEN authenticates the API calls.

The required list (`ci/required-jobs.toml`) and the workflows are read with `git show <sha>:<path>`
at the `main` sha the API reports, never from the working tree, so a branch cannot edit its own
list. `origin/main` is fetched and must equal that sha, else exit 2 ("stale origin/main").

- `contexts`: the merge tier's GitHub check names as a JSON list, for branch protection. A job
  is named by its literal `name` or its key; a matrix job gets one name per leg,
  `key (v1, v2)`, in axis order. Supported matrix shapes: list axes, plus `include` entries that
  only add keys to existing legs. An `exclude`, an `include` that adds a leg or a templated
  `name` exits 2 naming the job.
- `verify --tier merge`: per merge workflow, the newest `pull_request` run on head H has every
  derived name with conclusion `success` (skipped, neutral, cancelled and missing are red) on
  `run_attempt` 1, unless `--allow-rerun` names the workflow; the reason is printed for the gate
  log.
- `verify --tier nightly`: per workflow with a `schedule` trigger on main, the newest `schedule`
  run is on the main sha, at most 26 hours old, `completed` with conclusion `success`, and has
  every job the tree derives for that workflow with conclusion `success`. All job pages are read.
  A job skipped on schedule counts as not green.

Exit 0: everything checks. Exit 1: a red, missing or stale item. Exit 2: usage, derivation or API
error. An error is never a pass. Output names workflows, jobs and shas only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import chain, product
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))  # the sibling module, also run by path
from gate_tree_proof import github_fetch

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

REPO = "louiskeep/decoy-engine"
MAX_NIGHTLY_AGE = timedelta(hours=26)
WORKFLOW_DIR = ".github/workflows/"
REGISTRY = "ci/required-jobs.toml"
RUNS = 100
MAX_LIST_PAGES = 100

Fetch = Callable[[str], Any]
Git = Callable[[Sequence[str]], str]


class CheckError(Exception):
    """Usage, derivation or API trouble. The CLI maps it to exit 2."""


class DerivationError(CheckError):
    """A job whose check names cannot be derived with certainty."""


def workflow_on(doc: dict[Any, Any]) -> Any:
    return doc[True] if True in doc else doc.get("on")  # YAML 1.1 reads a bare `on:` key as True


def workflow_events(doc: dict[Any, Any]) -> set[str]:
    """The trigger names of a workflow: the scalar, list and mapping forms of `on`."""
    on = workflow_on(doc)
    if isinstance(on, str):
        return {on}
    if isinstance(on, (list, dict)):
        return set(on)
    raise ValueError("workflow has no readable `on` trigger")


def _leg_text(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def job_check_names(key: str, job: dict[str, Any]) -> list[str]:
    """The check names GitHub reports for a job: one, or one per matrix leg."""
    name = job.get("name", key)
    if not isinstance(name, str) or "${{" in name:
        raise DerivationError(f"{key}: templated name")
    matrix = job.get("strategy", {}).get("matrix")
    if matrix is None:
        return [name]
    if not isinstance(matrix, dict):
        raise DerivationError(f"{key}: matrix is not a mapping")
    if "exclude" in matrix:
        raise DerivationError(f"{key}: matrix exclude is not supported")
    axes = {k: v for k, v in matrix.items() if k != "include"}
    for axis, values in axes.items():
        if not isinstance(values, list) or any(isinstance(v, (list, dict)) for v in values):
            raise DerivationError(f"{key}: matrix axis {axis!r} is not a list of scalars")
    if not axes:
        raise DerivationError(f"{key}: matrix include adds a leg")
    legs = [dict(zip(axes, combo, strict=True)) for combo in product(*axes.values())]
    for extra in matrix.get("include", []):
        fixed = {k: v for k, v in extra.items() if k in axes}
        if not any(all(leg[k] == v for k, v in fixed.items()) for leg in legs):
            raise DerivationError(f"{key}: matrix include adds a leg")
    return [f"{name} ({', '.join(_leg_text(v) for v in leg.values())})" for leg in legs]


def merge_contexts(
    required: dict[str, Any], workflows: dict[str, dict[Any, Any]]
) -> dict[str, list[str]]:
    """Workflow file -> the check names of its merge jobs, in registry order."""
    out: dict[str, list[str]] = {}
    for entry in required["merge"]:
        wf, doc = entry["workflow"], workflows.get(entry["workflow"])
        if doc is None:
            raise CheckError(f"{wf}: merge workflow does not exist at the main sha")
        names: list[str] = []
        failed = None
        for key in entry["jobs"]:
            if key not in doc["jobs"]:
                raise CheckError(f"{wf}:{key}: merge job does not exist at the main sha")
            try:
                names += job_check_names(key, doc["jobs"][key])
            except DerivationError as e:
                failed = str(e)  # raised below, outside the except, so no __context__ is kept
                break
        if failed is not None:
            raise DerivationError(f"{wf}:{failed}")
        out[wf] = names
    return out


@dataclass(frozen=True)
class MainTree:
    sha: str
    required: dict[str, Any]
    workflows: dict[str, dict[Any, Any]]


def read_tree(sha: str, git: Git) -> MainTree:
    """The registry and every workflow as committed at `sha`."""
    required = tomllib.loads(git(["show", f"{sha}:{REGISTRY}"]))
    workflows = {}
    for path in git(["ls-tree", "--name-only", sha, WORKFLOW_DIR]).split():
        if path.endswith((".yml", ".yaml")):
            workflows[path.removeprefix(WORKFLOW_DIR)] = yaml.safe_load(
                git(["show", f"{sha}:{path}"])
            )
    return MainTree(sha, required, workflows)


def main_tree(repo: str, fetch: Fetch, git: Git) -> MainTree:
    """The main tip from the API, checked against a fresh `origin/main`."""
    sha = fetch(f"repos/{repo}/branches/main")["commit"]["sha"]
    print(f"main sha {sha}")
    git(["fetch", "origin", "main"])
    local = git(["rev-parse", "origin/main"]).strip()
    if local != sha:
        raise CheckError(f"stale origin/main: the API main is {sha}, local origin/main is {local}")
    return read_tree(sha, git)


def _newest(runs: list[dict[str, Any]], **want: Any) -> dict[str, Any] | None:
    hits = [r for r in runs if all(r[k] == v for k, v in want.items())]
    return max(hits, key=lambda r: r["created_at"], default=None)


def _read_listing(
    fetch: Fetch,
    page_path: Callable[[int], str],
    list_field: str,
    item_fields: dict[str, tuple[type, ...]],
    *,
    run_id: int | None = None,
) -> list[dict[str, Any]]:
    """Read one strict, finite API listing and cross-check its declared total."""

    def error(message: str) -> CheckError:
        prefix = f"run {run_id}: " if run_id is not None else ""
        return CheckError(prefix + message)

    items: list[dict[str, Any]] = []
    ids: set[int] = set()
    total: int | None = None
    for page in range(1, MAX_LIST_PAGES + 1):
        response = fetch(page_path(page))
        if type(response) is not dict:
            raise error(f"listing fields total_count and {list_field} have wrong type")
        count = response.get("total_count")
        batch = response.get(list_field)
        if type(count) is not int:
            raise error("listing field total_count has wrong type")
        if type(batch) is not list:
            raise error(f"listing field {list_field} has wrong type")
        if total is None:
            total = count
        elif count != total:
            raise error("listing field total_count changed between pages")
        if not batch:
            break
        for item in batch:
            if type(item) is not dict:
                raise error(f"listing field {list_field} has a non-mapping item")
            for field, allowed_types in item_fields.items():
                if field not in item or type(item[field]) not in allowed_types:
                    raise error(f"listing field {field} has wrong type")
            item_id = item["id"]
            if item_id in ids:
                raise error("listing field id is not distinct")
            ids.add(item_id)
            items.append(item)
    else:
        raise error(f"listing exceeded page count {MAX_LIST_PAGES}")

    if total != len(ids):
        raise error(f"listing read {len(ids)} distinct items against total_count {total}")
    return items


def _runs(repo: str, workflow: str, query: str, fetch: Fetch) -> list[dict[str, Any]]:
    """Every workflow run page, with the fields consumed by both verification tiers."""
    fields = {
        "id": (int,),
        "event": (str,),
        "head_sha": (str,),
        "run_attempt": (int,),
        "created_at": (str,),
        "status": (str,),
        "conclusion": (str, type(None)),
    }
    return _read_listing(
        fetch,
        lambda page: (
            f"repos/{repo}/actions/workflows/{workflow}/runs?{query}&per_page={RUNS}&page={page}"
        ),
        "workflow_runs",
        fields,
    )


def _jobs(repo: str, run: dict[str, Any], fetch: Fetch) -> dict[str, list[str | None]]:
    """Every job of the run, all pages; any read that is not exact is an error, never a list."""
    jobs = _read_listing(
        fetch,
        lambda page: (
            f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100&page={page}"
        ),
        "jobs",
        {"id": (int,), "name": (str,), "conclusion": (str, type(None))},
        run_id=run["id"],
    )
    by_name: dict[str, list[str | None]] = {}
    for job in jobs:
        by_name.setdefault(job["name"], []).append(job["conclusion"])
    return by_name


def verify_merge(
    repo: str, head: str, tree: MainTree, fetch: Fetch, allow_rerun: dict[str, str]
) -> list[str]:
    """Problems that keep `head` from merging; empty when every merge job is green first time."""
    problems = []
    for wf, names in merge_contexts(tree.required, tree.workflows).items():
        runs = _runs(repo, wf, f"event=pull_request&head_sha={head}", fetch)
        run = _newest(runs, event="pull_request", head_sha=head)
        if run is None:
            problems.append(f"{wf}: no pull_request run on {head}")
            continue
        if run["run_attempt"] != 1:
            if wf in allow_rerun:
                print(f"{wf}: attempt {run['run_attempt']} allowed: {allow_rerun[wf]}")
            else:
                problems.append(f"{wf}: run attempt {run['run_attempt']}, first attempt required")
        got = _jobs(repo, run, fetch)
        for name in names:
            if name not in got:
                problems.append(f"{wf}: check {name!r} is missing")
            elif set(got[name]) != {"success"}:
                problems.append(f"{wf}: check {name!r} is not green")
    return problems


def verify_nightly(repo: str, tree: MainTree, fetch: Fetch, now: datetime) -> list[str]:
    """Problems with last night's runs; empty when all are fresh and green on the main tip."""
    problems = []
    for wf, doc in sorted(tree.workflows.items()):
        if "schedule" not in workflow_events(doc):
            continue
        runs = _runs(repo, wf, "event=schedule", fetch)
        run = _newest(runs, event="schedule")
        if run is None or run["head_sha"] != tree.sha:
            problems.append(f"{wf}: no fresh run on the main sha")
            continue
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if now - created > MAX_NIGHTLY_AGE:
            problems.append(f"{wf}: no fresh run, the newest is older than 26 hours")
            continue
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            problems.append(f"{wf}: run is {run.get('status')}/{run.get('conclusion')}, not green")
            continue
        try:
            expected = [n for k, j in doc["jobs"].items() for n in job_check_names(k, j)]
        except DerivationError as e:
            problems.append(f"{wf}: cannot derive the expected jobs: {e}")
            continue
        got = _jobs(repo, run, fetch)
        if not got:
            problems.append(f"{wf}: the run has no jobs")
            continue
        problems += [f"{wf}: check {n!r} is missing" for n in expected if n not in got]
        problems += [
            f"{wf}: job {n!r} is not green" for n, c in got.items() if set(c) != {"success"}
        ]
    return problems


def _run_git(args: Sequence[str]) -> str:
    cmd = ["git", *args]  # fixed argv, no shell
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout  # noqa: S603


def _allow_rerun(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        wf, _, reason = item.partition("=")
        if not wf or not reason.strip():
            raise CheckError("--allow-rerun needs WORKFLOW=REASON")
        out[wf] = reason.strip()
    return out


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", REPO))
    sub = ap.add_subparsers(dest="cmd", required=True)
    ctx = sub.add_parser("contexts")
    ctx.add_argument("--ref", help="read this ref instead of a freshly fetched origin/main")
    ver = sub.add_parser("verify")
    ver.add_argument("--tier", choices=["merge", "nightly"], required=True)
    ver.add_argument("--sha")
    ver.add_argument("--allow-rerun", action="append", default=[], metavar="WORKFLOW=REASON")
    return ap


def _contexts(args: argparse.Namespace, git: Git) -> int:
    if args.ref:
        sha = args.ref
    else:
        git(["fetch", "origin", "main"])
        sha = git(["rev-parse", "origin/main"]).strip()
    print(f"ref {sha}", file=sys.stderr)
    tree = read_tree(sha, git)
    print(
        json.dumps(
            list(chain.from_iterable(merge_contexts(tree.required, tree.workflows).values()))
        )
    )
    return 0


def _verify(args: argparse.Namespace, fetch: Fetch, git: Git, now: datetime) -> int:
    if args.tier == "merge" and not args.sha:
        raise CheckError("--tier merge needs --sha")
    allow = _allow_rerun(args.allow_rerun)
    tree = main_tree(args.repo, fetch, git)
    if args.tier == "merge":
        problems = verify_merge(args.repo, args.sha, tree, fetch, allow)
    else:
        problems = verify_nightly(args.repo, tree, fetch, now)
    for line in problems:
        print(line)
    if not problems:
        print(f"{args.tier} tier green")
    return 1 if problems else 0


def main(
    argv: list[str] | None = None,
    fetch: Fetch | None = None,
    git: Git | None = None,
    now: datetime | None = None,
) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit:
        return 2
    git = git or _run_git
    try:
        if args.cmd == "contexts":
            return _contexts(args, git)
        fetch = fetch or github_fetch(os.environ.get("GH_TOKEN", ""))
        return _verify(args, fetch, git, now or datetime.now(timezone.utc))
    except CheckError as e:
        print(f"required_checks: {e}", file=sys.stderr)
    except Exception as e:
        print(
            f"required_checks: {type(e).__name__} while reading CI state",
            file=sys.stderr,
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
