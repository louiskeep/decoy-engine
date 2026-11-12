"""CI config sentry (R0 item 2): job timeouts and ruff pin alignment."""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

import yaml
from scripts.dev import gate_tree_proof
from scripts.dev.required_checks import workflow_events

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


def _lint_extra_ruff_pin() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    for req in data["project"]["optional-dependencies"]["lint"]:
        m = re.fullmatch(r"ruff==([\d.]+)", req.strip())
        if m:
            return m.group(1)
    raise AssertionError("no pinned ruff in the [lint] extra")


def test_ci_ruff_pin_matches_lint_extra() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    pins = set(re.findall(r"ruff==([\d.]+)", text))
    assert pins == {_lint_extra_ruff_pin()}, (pins, _lint_extra_ruff_pin())


def test_precommit_ruff_hook_matches_lint_extra() -> None:
    text = (ROOT / ".pre-commit-config.yaml").read_text()
    m = re.search(r"ruff-pre-commit\s*\n(?:\s*#.*\n)*\s*rev:\s*v([\d.]+)", text)
    assert m, "ruff pre-commit hook not found"
    assert m.group(1) == _lint_extra_ruff_pin()


WORKFLOWS = ROOT / ".github" / "workflows"


def selfhosted_violations(wf: dict) -> list[str]:
    """Self-hosted jobs run only on push and dispatch and must carry the owner-only guard.

    A PR or a schedule must never start one: gate-vm-watch starts a VM for any queued self-hosted
    job. Events are decoded in one place, scripts.dev.required_checks.workflow_events."""
    triggers = workflow_events(wf)
    out = []
    for name, job in wf["jobs"].items():
        if "self-hosted" not in str(job.get("runs-on", "")):
            continue
        bad = triggers - {"push", "workflow_dispatch"}
        if bad:
            out.append(f"{name}: self-hosted job triggered by {sorted(bad)}")
        guard = str(job.get("if", ""))
        if "github.actor ==" not in guard or "github.event.repository.fork" not in guard:
            out.append(f"{name}: self-hosted job lacks the actor and fork guard")
    return out


def test_self_hosted_jobs_are_pr_free_and_actor_guarded() -> None:
    bad = {
        p.name: selfhosted_violations(yaml.safe_load(p.read_text()))
        for p in WORKFLOWS.glob("*.yml")
    }
    assert {k: v for k, v in bad.items() if v} == {}


def test_seeded_unsafe_selfhosted_workflows_are_caught() -> None:
    unsafe = yaml.safe_load(
        "on: [pull_request]\njobs:\n  g:\n    runs-on: [self-hosted, macos]\n    steps: []\n"
    )
    found = selfhosted_violations(unsafe)
    assert any("pull_request" in f for f in found)
    assert any("guard" in f for f in found)
    target = yaml.safe_load(
        "on: [pull_request_target]\njobs:\n  g:\n    runs-on: self-hosted\n"
        "    if: github.actor == 'x' && !github.event.repository.fork\n    steps: []\n"
    )
    assert selfhosted_violations(target) == [
        "g: self-hosted job triggered by ['pull_request_target']"
    ]


def _gate_jobs() -> tuple[dict, dict]:
    wf = yaml.safe_load((WORKFLOWS / "gate-selfhosted.yml").read_text())
    assert set(wf["jobs"]) == {"fast", "tree-proof", "gate"}
    return wf["jobs"]["fast"], wf["jobs"]["gate"]


def _checkout(job: dict) -> dict:
    return next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))


def test_selfhosted_gate_runs_gates_under_the_shared_lock() -> None:
    _, job = _gate_jobs()
    assert job["runs-on"] == ["self-hosted", "decoy-gate"]
    assert "timeout-minutes" in job
    assert yaml.safe_load((WORKFLOWS / "gate-selfhosted.yml").read_text())["permissions"] == {
        "contents": "read"
    }
    assert "secrets." not in (WORKFLOWS / "gate-selfhosted.yml").read_text()
    assert _checkout(job)["with"]["persist-credentials"] is False
    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert "flock ~/.cache/pytest-one.lock scripts/dev/gates.sh" in runs
    # Host-only failures that main shares must not fail every dispatch run.
    assert "scripts/dev/gates.sh --keep-going $mode" in runs
    assert "mode=--baseline" in runs
    assert workflow_events(yaml.safe_load((WORKFLOWS / "gate-selfhosted.yml").read_text())) == {
        "push",
        "workflow_dispatch",
    }


def test_full_gate_runs_only_on_dispatch_and_main_in_two_shards() -> None:
    _, job = _gate_jobs()
    guard = str(job["if"])
    # The expensive self-hosted gate: manual dispatch or a push to main, never a feature push.
    assert "github.event_name == 'workflow_dispatch'" in guard
    assert "github.ref == 'refs/heads/main'" in guard
    assert "github.repository_owner == 'louiskeep'" in guard
    assert "github.actor == 'louiskeep'" in guard
    assert "!github.event.repository.fork" in guard
    assert job["strategy"]["matrix"]["shard"] == [1, 2]
    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert '--shard "${{ matrix.shard }}/2"' in runs
    # On main HEAD equals origin/main, so an impact selection is empty: main runs the full suite,
    # and without --baseline (main against itself would call every failure shared).
    assert "mode=--full" in runs
    assert "mode=--baseline" in runs
    assert 'if [ "$GATE_MODE" = full ]; then mode=--full; else mode=--baseline; fi' in runs
    gate_step = next(s for s in job["steps"] if s.get("name") == "Gate")
    assert gate_step["env"]["GATE_MODE"] == GATE_MODE_EXPR
    # The exact command line: lock, keep-going, mode, step list, shard. Shard 1 owns the lint,
    # type, sentry and licence steps; every other shard runs only its slice of the tests.
    assert (
        "flock ~/.cache/pytest-one.lock scripts/dev/gates.sh --keep-going $mode $steps"
        ' --shard "${{ matrix.shard }}/2"'
    ) in runs
    # The step list has one owner, gates.sh: shard 1 passes no --steps, the others only tests.
    assert 'if [ "${{ matrix.shard }}" = 1 ]; then steps=; else steps="--steps tests"; fi' in runs
    assert "env,ruff" not in runs


def test_fast_check_is_hosted_owner_guarded_and_push_only() -> None:
    fast, _ = _gate_jobs()
    # GitHub-hosted: free on a public repo, and never a self-hosted runner for feature pushes.
    assert fast["runs-on"] == "ubuntu-latest"
    assert "self-hosted" not in str(fast["runs-on"])
    assert "timeout-minutes" in fast
    guard = str(fast["if"])
    assert "github.event_name == 'push'" in guard
    assert "github.repository_owner == 'louiskeep'" in guard
    assert "github.actor == 'louiskeep'" in guard
    assert "!github.event.repository.fork" in guard
    assert _checkout(fast)["with"]["persist-credentials"] is False
    runs = " ".join(step.get("run", "") for step in fast["steps"])
    assert "scripts/dev/gates.sh --keep-going --steps env,ruff,format,mypy,sentry" in runs
    assert "tests" not in runs.split("--steps")[1].split()[0].split(",")


GATE_BRANCHES_IGNORE = [
    "plan/**",
    "feat/v2-**",
    "plan/v2-**",
    "feat/v1plus-**",
    "plan/v1plus-**",
]


def test_selfhosted_gate_push_filters_are_pinned() -> None:
    # Exact lists: a later edit must not silently widen or drop them.
    wf = yaml.safe_load((WORKFLOWS / "gate-selfhosted.yml").read_text())
    push = (wf.get(True) or wf["on"])["push"]
    assert push["branches-ignore"] == GATE_BRANCHES_IGNORE
    # No path filter of any kind: sentries read files under docs/ and *.md, and GitHub path
    # filters stop evaluating after 3,000 changed files.
    assert not {"branches", "paths", "paths-ignore"} & set(push)
    for ref in ("main", "refs/heads/main"):
        assert not any(fnmatch.fnmatchcase(ref, pat) for pat in push["branches-ignore"])


# --- Tree proof: a main push skips its gate only on a byte-identical tree that passed full. ---

GATE_MODE_EXPR = (
    "${{ (github.event_name == 'push' || inputs.mode == 'full') && 'full' || 'baseline' }}"
)


def _tree_proof_job() -> dict:
    return yaml.safe_load((WORKFLOWS / "gate-selfhosted.yml").read_text())["jobs"]["tree-proof"]


def test_dispatch_mode_input_defaults_to_baseline_and_names_the_gate_jobs() -> None:
    wf = yaml.safe_load((WORKFLOWS / "gate-selfhosted.yml").read_text())
    mode = (wf.get(True) or wf["on"])["workflow_dispatch"]["inputs"]["mode"]
    assert (mode["type"], mode["options"], mode["default"]) == (
        "choice",
        ["baseline", "full"],
        "baseline",
    )
    _, gate = _gate_jobs()
    # The job name and the gates.sh mode come from one expression, so a job named "full" ran --full.
    assert gate["name"] == "gate ${{ matrix.shard }}/2 " + GATE_MODE_EXPR
    names = {f"gate {s}/2 full" for s in gate["strategy"]["matrix"]["shard"]}
    assert names == gate_tree_proof.FULL_GATE_JOBS


def test_tree_proof_job_is_hosted_main_push_only_and_read_only() -> None:
    job = _tree_proof_job()
    assert job["runs-on"] == "ubuntu-latest"
    assert "timeout-minutes" in job
    guard = str(job["if"])
    for part in (
        "github.event_name == 'push'",
        "github.ref == 'refs/heads/main'",
        "github.repository_owner == 'louiskeep'",
        "github.actor == 'louiskeep'",
        "!github.event.repository.fork",
    ):
        assert part in guard
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert _checkout(job)["with"]["persist-credentials"] is False
    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert 'tree=$(git rev-parse "HEAD^{tree}")' in runs
    assert "scripts/dev/gate_tree_proof.py" in runs and '--tree "$tree"' in runs


def test_gate_skips_only_on_an_explicit_proof() -> None:
    _, gate = _gate_jobs()
    assert gate["needs"] == "tree-proof"
    guard = str(gate["if"])
    # !cancelled(): tree-proof is skipped on a dispatch and may fail; neither may skip the gate.
    assert guard.startswith("!cancelled() && needs.tree-proof.outputs.skip != 'true' && ")


REPO = "louiskeep/decoy-engine"
TREE = "a" * 40


def _fake_api(runs: list[dict], trees: dict[str, str], jobs: dict[int, list[tuple[str, str]]]):
    def fetch(path: str) -> dict:
        if "/actions/workflows/gate-selfhosted.yml/runs?" in path:
            assert "event=workflow_dispatch" in path and "status=success" in path
            return {"workflow_runs": runs}
        if "/git/commits/" in path:
            return {"tree": {"sha": trees[path.rsplit("/", 1)[1]]}}
        run_id = int(path.split("/actions/runs/")[1].split("/")[0])
        return {"jobs": [{"name": n, "conclusion": c} for n, c in jobs[run_id]]}

    return fetch


def _run(run_id: int, sha: str, **over: object) -> dict:
    run = {
        "id": run_id,
        "head_sha": sha,
        "event": "workflow_dispatch",
        "conclusion": "success",
        "head_repository": {"full_name": REPO},
        "triggering_actor": {"login": "louiskeep"},
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
    }
    run.update(over)
    return run


GREEN_FULL = [("gate 1/2 full", "success"), ("gate 2/2 full", "success")]


def test_main_gate_skips_when_the_tree_matches_a_green_full_run() -> None:
    fetch = _fake_api([_run(7, "s7")], {"s7": TREE}, {7: GREEN_FULL})
    assert gate_tree_proof.decide(REPO, TREE, fetch) == [
        "skip=true",
        f"proof_run=https://github.com/{REPO}/actions/runs/7",
    ]


def test_main_gate_runs_without_an_identical_green_full_run() -> None:
    other = "b" * 40
    cases = {
        "no runs": ([], {}, {}),
        "different tree": ([_run(1, "s1")], {"s1": other}, {1: GREEN_FULL}),
        "baseline mode": (
            [_run(2, "s2")],
            {"s2": TREE},
            {2: [("gate 1/2 baseline", "success"), ("gate 2/2 baseline", "success")]},
        ),
        "one shard red": (
            [_run(3, "s3")],
            {"s3": TREE},
            {3: [("gate 1/2 full", "success"), ("gate 2/2 full", "failure")]},
        ),
        "one shard only": ([_run(4, "s4")], {"s4": TREE}, {4: GREEN_FULL[:1]}),
        "other actor": (
            [_run(5, "s5", triggering_actor={"login": "someone"})],
            {"s5": TREE},
            {5: GREEN_FULL},
        ),
        "fork head": (
            [_run(6, "s6", head_repository={"full_name": "x/decoy-engine"})],
            {"s6": TREE},
            {6: GREEN_FULL},
        ),
        "push event": ([_run(8, "s8", event="push")], {"s8": TREE}, {8: GREEN_FULL}),
        "run failed": ([_run(9, "s9", conclusion="failure")], {"s9": TREE}, {9: GREEN_FULL}),
    }
    for name, (runs, trees, jobs) in cases.items():
        assert gate_tree_proof.decide(REPO, TREE, _fake_api(runs, trees, jobs)) == ["skip=false"], (
            name
        )


def test_a_failed_lookup_runs_the_gate() -> None:
    def broken(path: str) -> dict:
        raise OSError("api down")

    assert gate_tree_proof.decide(REPO, TREE, broken) == ["skip=false"]
