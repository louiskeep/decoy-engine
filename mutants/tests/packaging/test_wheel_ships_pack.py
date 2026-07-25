"""DE-07 regression gate: the built artifacts MUST ship the default ML pack.

Before this fix a clean wheel packaged only ``src/decoy_engine`` and omitted the
pack (which lives under ``docs/v2/ml/packs/lgbm-v1``), so ``classify_fields``
silently returned ``None`` in every real install. The pyproject ``force-include``
copies the pack under the installed package at ``decoy_engine/model_packs/`` and
``classify._default_pack_dir()`` resolves it via ``importlib.resources``.

This test builds BOTH real artifacts (``python -m build`` with no ``--wheel``,
which produces the sdist AND then a wheel FROM that sdist -- the actual PyPI
path) and asserts membership on both, because:
  - a config-only check (does the force-include line exist) would not catch a
    build backend that silently drops the mapping, and
  - the wheel force-include reads ``docs/v2/ml/packs/lgbm-v1`` from the build
    context, so a wheel built from an sdist that omits that path would fail (or
    ship packless) -- the sdist membership is load-bearing and must be gated.

Marked ``packaging`` and run only in the dedicated ``packaging-gate`` CI job;
importorskips when the ``build`` frontend is absent (the default loop excludes
the marker AND does not install ``build``).
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

#: Files that MUST appear inside the sdist (suffix match; the tar prepends a
#: ``decoy_engine-<version>/`` root). These are the force-include SOURCE the
#: wheel-from-sdist build depends on -- if the sdist omits them, the PyPI path
#: breaks even though a direct-from-source wheel build would still succeed.
_REQUIRED_SDIST_SUFFIXES = (
    "docs/v2/ml/packs/lgbm-v1/manifest.json",
    "docs/v2/ml/packs/lgbm-v1/model.joblib",
)


def test_sdist_and_wheel_ship_model_pack_and_py_typed(tmp_path: Path) -> None:
    """The full build (sdist + wheel-from-sdist) ships the pack + py.typed.

    Building without ``--wheel`` exercises the real PyPI path: hatchling builds
    the sdist, then builds the wheel FROM the unpacked sdist. This proves both
    that the sdist carries the force-include source and that the resulting wheel
    contains the pack -- the two properties DE-07 requires ("install/test both
    artifacts").
    """
    result = subprocess.run(  # noqa: S603 -- static argv (sys.executable + literals), no untrusted input
        [sys.executable, "-m", "build", "--outdir", str(tmp_path)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"full build failed (rc={result.returncode}).\n"
        f"stdout:\n{result.stdout[-2000:]}\n\nstderr:\n{result.stderr[-2000:]}"
    )

    wheels = list(tmp_path.glob("*.whl"))
    sdists = list(tmp_path.glob("*.tar.gz"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    assert len(sdists) == 1, f"expected exactly one sdist, got {sdists}"

    # The wheel (built FROM the sdist) must carry the packaged pack + py.typed.
    with zipfile.ZipFile(wheels[0]) as wheel:
        wheel_names = set(wheel.namelist())
    missing_wheel = [m for m in _REQUIRED_WHEEL_MEMBERS if m not in wheel_names]
    assert not missing_wheel, (
        f"wheel {wheels[0].name} is missing required members: {missing_wheel}.\n"
        "The [tool.hatch.build.targets.wheel.force-include] mapping or the "
        "py.typed marker regressed; classify_fields would return None in a "
        "real install (DE-07)."
    )

    # The sdist must carry the force-include SOURCE, or wheel-from-sdist breaks.
    import tarfile

    with tarfile.open(sdists[0]) as sdist:
        sdist_names = sdist.getnames()
    missing_sdist = [
        suffix
        for suffix in _REQUIRED_SDIST_SUFFIXES
        if not any(name.endswith(suffix) for name in sdist_names)
    ]
    assert not missing_sdist, (
        f"sdist {sdists[0].name} is missing the force-include source: {missing_sdist}.\n"
        "A wheel built from this sdist (the PyPI path) would fail or ship packless. "
        "The pack under docs/v2/ml/packs/lgbm-v1 must remain in the sdist."
    )

    # Scratch must NOT leak into the sdist. hatchling's default selection sweeps
    # untracked working-tree content, so a build from a dirty dev checkout could
    # otherwise publish agent worktrees / the Hypothesis DB to PyPI. Gate it so a
    # regression (someone dropping the .gitignore / sdist-exclude entries) fails
    # here rather than at release time.
    leaked = sorted(
        name
        for name in sdist_names
        if "/.claude/" in name or name.endswith("/.claude") or "/.hypothesis/" in name
    )
    assert not leaked, (
        f"sdist {sdists[0].name} leaked dev scratch ({len(leaked)} entries, e.g. "
        f"{leaked[:3]}). Check the .gitignore and "
        "[tool.hatch.build.targets.sdist] exclude entries."
    )
