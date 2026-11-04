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
# Release and tag publishing keep their triggers; they are not merge gates.
TRIGGERED_ON_PURPOSE = {"gate-selfhosted.yml", "native-companion-release.yml"}


def _triggers(path: Path) -> set[str]:
    on = yaml.safe_load(path.read_text())
    return set((on.get(True) or on.get("on")).keys())  # YAML parses a bare `on:` key as True


def test_paid_runner_workflows_are_manual_dispatch_only() -> None:
    """X0a: the self-hosted gate is the merge gate; paid runners fire only on demand."""
    bad = {
        p.name: sorted(_triggers(p) - {"workflow_dispatch"})
        for p in sorted(WORKFLOWS.glob("*.yml"))
        if p.name not in TRIGGERED_ON_PURPOSE and _triggers(p) != {"workflow_dispatch"}
    }
    assert not bad, f"workflows with automatic triggers: {bad}"


def test_selfhosted_gate_runs_gates_under_the_shared_lock() -> None:
    wf = yaml.safe_load((WORKFLOWS / "gate-selfhosted.yml").read_text())
    (job,) = wf["jobs"].values()
    assert job["runs-on"] == ["self-hosted", "macos", "decoy-gate"]
    assert "timeout-minutes" in job
    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert "flock ~/.cache/pytest-one.lock scripts/dev/gates.sh" in runs
    assert {"push", "pull_request"} <= _triggers(WORKFLOWS / "gate-selfhosted.yml")
