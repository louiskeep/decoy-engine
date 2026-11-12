"""SCAN-ML M2: the classifier stack is a base dependency and CI proves a base-only install runs it.

Static guards over `pyproject.toml` and `ci.yml`. The behavior proof (AT-M2a) is the `base-install`
job, which runs `scripts/dev/base_install_smoke.py` in a venv with no extras. A hosted lane cannot
run inside this suite, so these tests pin the wiring the lane depends on.
"""

from __future__ import annotations

import sys

import yaml

from tests.sentry._ci_lock_scan import ROOT

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
# Exact pins, unchanged by the move: the frozen lgbm-v1 golden retrain gate depends on them.
CLASSIFIER_PINS = {"scikit-learn==1.7.2", "lightgbm==4.6.0"}


def _lock_env_extras(job: str) -> str:
    assert job in CI, f"ci.yml has no {job} job"
    steps = [s for s in CI[job]["steps"] if "lock-env" in s.get("uses", "")]
    assert len(steps) == 1, f"{job} must install through lock-env once"
    return str(steps[0].get("with", {}).get("extras", ""))


def test_classifier_stack_is_a_base_dependency_with_the_same_exact_pins() -> None:
    assert set(PYPROJECT["project"]["dependencies"]) >= CLASSIFIER_PINS


def test_the_ml_extra_is_gone() -> None:
    assert "ml" not in PYPROJECT["project"]["optional-dependencies"]
    markers = PYPROJECT["tool"]["pytest"]["ini_options"]["markers"]
    ml_marker = [m for m in markers if m.startswith("ml:")]
    assert len(ml_marker) == 1
    # Fail on any wording that points at an extra, not just the old literal.
    text = ml_marker[0].lower()
    assert "extra" not in text, ml_marker[0]
    assert "[ml]" not in text and "base dependencies" in text


def test_ml_gate_needs_no_ml_extra() -> None:
    assert "ml" not in _lock_env_extras("ml-gate").split()


def test_base_install_lane_installs_no_extras_and_runs_the_smoke() -> None:
    assert _lock_env_extras("base-install") == ""
    run = "\n".join(str(s.get("run", "")) for s in CI["base-install"]["steps"])
    assert "scripts/dev/base_install_smoke.py" in run
    # `bootstrap: "true"` would quietly install extras and void the no-extras proof.
    for step in CI["base-install"]["steps"]:
        if "lock-env" in step.get("uses", ""):
            assert str(step.get("with", {}).get("bootstrap", "false")).lower() != "true"


def test_wheel_still_force_includes_the_model_pack() -> None:
    include = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert include["docs/v2/ml/packs/lgbm-v1"] == "decoy_engine/model_packs/lgbm-v1"
    pack = ROOT / "docs/v2/ml/packs/lgbm-v1"
    assert (pack / "model.joblib").is_file()
    assert list(pack.glob("manifest.*")), "the pack has no manifest"
