"""DE-07 regression gate: the built wheel MUST ship the default ML model pack.

Before this fix a clean wheel packaged only ``src/decoy_engine`` and omitted the
pack (which lives under ``docs/v2/ml/packs/lgbm-v1``), so ``classify_fields``
silently returned ``None`` in every real install. The pyproject ``force-include``
copies the pack under the installed package at ``decoy_engine/model_packs/`` and
``classify._default_pack_dir()`` resolves it via ``importlib.resources``.

This test builds an actual wheel and asserts membership, because a config-only
check (does the force-include line exist) would not catch a build backend that
silently drops the mapping. It is marked ``packaging`` and runs in the dedicated
``packaging-gate`` CI job; it importorskips when the ``build`` frontend is absent
(the default local loop does not install it).
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.packaging

pytest.importorskip("build", reason="wheel-build frontend not installed (packaging-gate only)")

_REPO_ROOT = Path(__file__).parents[2]

#: Files that MUST appear inside the built wheel. Paths are wheel-internal
#: (relative to the archive root), i.e. under the installed ``decoy_engine``.
_REQUIRED_WHEEL_MEMBERS = (
    "decoy_engine/model_packs/lgbm-v1/manifest.json",
    "decoy_engine/model_packs/lgbm-v1/model.joblib",
    "decoy_engine/py.typed",
)


def test_wheel_ships_model_pack_and_py_typed(tmp_path: Path) -> None:
    """A freshly built wheel contains the default pack + the py.typed marker."""
    # Build ONLY the wheel (skip the sdist, which is large and slow) directly
    # from the source tree, mirroring what pip/uv produce for a binary install.
    result = subprocess.run(  # noqa: S603 -- static argv (sys.executable + literals), no untrusted input
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"wheel build failed (rc={result.returncode}).\n"
        f"stdout:\n{result.stdout[-2000:]}\n\nstderr:\n{result.stderr[-2000:]}"
    )

    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"

    names = set(zipfile.ZipFile(wheels[0]).namelist())
    missing = [m for m in _REQUIRED_WHEEL_MEMBERS if m not in names]
    assert not missing, (
        f"wheel {wheels[0].name} is missing required members: {missing}.\n"
        "The [tool.hatch.build.targets.wheel.force-include] mapping or the "
        "py.typed marker regressed; classify_fields would return None in a "
        "real install (DE-07)."
    )
