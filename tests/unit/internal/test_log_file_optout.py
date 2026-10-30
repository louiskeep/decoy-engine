"""Importing the engine must be able to leave NO file behind (F26 local-cleanup is stateless).

`import decoy_engine` instantiates module-level strategies that configure the engine logger, whose
default is a rotating file under ./logs. DECOY_ENGINE_LOG_FILE="" opts out; unset keeps the default."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys


def _import_in(tmp_path, **env):
    import decoy_engine

    # run the subprocess against the SAME source tree this test process imported
    src = str(pathlib.Path(decoy_engine.__file__).resolve().parents[1])
    full = {**os.environ, **env, "PYTHONPATH": src}
    full.pop("DECOY_ENGINE_LOG_FILE", None) if "DECOY_ENGINE_LOG_FILE" not in env else None
    subprocess.run(
        [sys.executable, "-c", "import decoy_engine.retention"],
        cwd=tmp_path,
        env=full,
        check=True,
        capture_output=True,
    )
    return sorted(p.name for p in tmp_path.iterdir())


def test_empty_env_var_disables_the_log_file(tmp_path):
    assert _import_in(tmp_path, DECOY_ENGINE_LOG_FILE="") == []


def test_default_is_unchanged(tmp_path):
    assert _import_in(tmp_path) == ["logs"]


def test_env_var_can_redirect_the_log_file(tmp_path):
    target = tmp_path / "elsewhere" / "e.log"
    names = _import_in(tmp_path, DECOY_ENGINE_LOG_FILE=str(target))
    assert names == ["elsewhere"] and target.exists()
