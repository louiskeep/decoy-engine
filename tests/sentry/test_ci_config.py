"""CI config sentry (R0 item 2): job timeouts and ruff pin alignment."""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


def test_every_ci_job_has_a_timeout() -> None:
    missing = [name for name, job in _workflow()["jobs"].items() if "timeout-minutes" not in job]
    assert not missing, f"ci.yml jobs without timeout-minutes: {missing}"


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
# Release and tag publishing keep their triggers; they are not merge gates. The two PR checks
# read pull_request context (base_ref, PR body, labels) and cannot run from a manual dispatch.
TRIGGERED_ON_PURPOSE = {
    "gate-selfhosted.yml",
    "native-companion-release.yml",
    "compat-preflight.yml",
    "regression-proof.yml",
}


def _trigger_names(wf: dict) -> set[str]:
    on = wf.get(True) or wf.get("on")  # YAML parses a bare `on:` key as True
    if isinstance(on, str):
        return {on}
    return set(on)  # a mapping yields its keys, a list its items


def _triggers(path: Path) -> set[str]:
    return _trigger_names(yaml.safe_load(path.read_text()))


def test_paid_runner_workflows_are_manual_dispatch_only() -> None:
    """X0a: the self-hosted gate is the merge gate; paid runners fire only on demand."""
    bad = {
        p.name: sorted(_triggers(p) - {"workflow_dispatch"})
        for p in sorted(WORKFLOWS.glob("*.yml"))
        if p.name not in TRIGGERED_ON_PURPOSE and _triggers(p) != {"workflow_dispatch"}
    }
    assert not bad, f"workflows with automatic triggers: {bad}"


def selfhosted_violations(wf: dict) -> list[str]:
    """Self-hosted jobs must not run on PR triggers and must carry the owner-only guard."""
    triggers = _trigger_names(wf)
    out = []
    for name, job in wf["jobs"].items():
        if "self-hosted" not in str(job.get("runs-on", "")):
            continue
        bad = triggers & {"pull_request", "pull_request_target"}
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
    assert set(wf["jobs"]) == {"fast", "gate"}
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
    assert _triggers(WORKFLOWS / "gate-selfhosted.yml") == {"push", "workflow_dispatch"}


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
    assert (
        'if [ "${{ github.event_name }}" = push ]; then mode=--full; else mode=--baseline; fi'
        in runs
    )
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
