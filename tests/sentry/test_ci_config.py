"""CI config sentry (R0 item 2): job timeouts and ruff pin alignment."""

from __future__ import annotations

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


def test_selfhosted_gate_runs_gates_under_the_shared_lock() -> None:
    wf = yaml.safe_load((WORKFLOWS / "gate-selfhosted.yml").read_text())
    (job,) = wf["jobs"].values()
    assert job["runs-on"] == ["self-hosted", "macos", "decoy-gate"]
    assert "timeout-minutes" in job
    assert wf["permissions"] == {"contents": "read"}
    assert "secrets." not in (WORKFLOWS / "gate-selfhosted.yml").read_text()
    checkout = next(
        s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["persist-credentials"] is False
    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert "flock ~/.cache/pytest-one.lock scripts/dev/gates.sh" in runs
    assert _triggers(WORKFLOWS / "gate-selfhosted.yml") == {"push", "workflow_dispatch"}
