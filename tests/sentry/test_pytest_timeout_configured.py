"""A hung test must fail with a stack dump, not stall the suite.

A full engine run once hung for 70 minutes on a single test. pytest-timeout bounds every test; this
sentry fails when the plugin is not loaded or the repo's per-test limit is missing or zero, and a
subprocess run proves the plugin turns a sleeping test into a failure that carries its stack.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_plugin_is_loaded(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.pluginmanager.hasplugin("timeout"), (
        "pytest-timeout is not installed or loaded; it is part of the `dev` extra"
    )


def test_default_timeout_is_configured(pytestconfig: pytest.Config) -> None:
    raw = pytestconfig.getini("timeout")
    assert raw, "[tool.pytest.ini_options] must set a non-zero `timeout` (seconds per test)"
    assert float(raw) > 0
    # Unset on purpose: the plugin picks signal on POSIX and thread on Windows by itself.
    assert not pytestconfig.getini("timeout_method")


def test_pyproject_declares_the_limit() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "\ntimeout = " in text and "\ntimeout_method" not in text


def test_a_test_past_its_limit_fails_with_its_stack(tmp_path: Path) -> None:
    (tmp_path / "test_hang.py").write_text(
        textwrap.dedent(
            """
            import time

            def stuck_helper():
                time.sleep(30)

            def test_hang():
                stuck_helper()
            """
        ),
        encoding="utf-8",
    )
    run = subprocess.run(  # noqa: S603 -- fixed argv, this interpreter
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-o",
            "timeout=1",
            "-p",
            "pytest_timeout",
            str(tmp_path / "test_hang.py"),
        ],
        cwd=tmp_path,
        # Autoload off and the plugin named, so the run works whether or not the parent disabled
        # autoload (naming it with autoload on would register it twice).
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    assert "Timeout" in run.stdout
    assert "stuck_helper" in run.stdout
