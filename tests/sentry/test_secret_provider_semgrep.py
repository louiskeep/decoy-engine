"""F13 structural leak sentry: run the Semgrep ruleset when Semgrep is installed.

CI always runs it (`.github/workflows/security.yml`, job `secret-provider-semgrep`); locally this
test skips when `semgrep` is not on PATH so the base dev env needs no extra tool.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / ".semgrep" / "f13-secret-providers.yml"

pytestmark = pytest.mark.skipif(shutil.which("semgrep") is None, reason="semgrep not installed")


def _scan(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["semgrep", "--config", str(RULES), "--metrics=off", "--error", str(target)],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def test_engine_source_is_clean() -> None:
    result = _scan(ROOT / "src" / "decoy_engine")
    assert result.returncode == 0, result.stdout[-2000:]
