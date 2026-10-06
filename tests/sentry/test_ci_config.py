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
