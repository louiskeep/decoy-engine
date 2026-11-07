"""Static guard: the lock's certified DP profile is a row of ``_CERTIFIED_STACKS``.

``tests/_dp_cert.py`` turns a failed ``check_fit_environment`` into a skip of every ``dp_certified``
test, so a lock change that skips the re-pin would hide those tests on the VMs and the Mac. This
guard computes the profile fingerprint from ``uv.lock`` alone (``uv export`` plus PEP 508 markers
evaluated for the certified platform) and fails on every gate host instead. It never skips.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from packaging.markers import Marker
from packaging.utils import canonicalize_name

from decoy_engine.quality import dp_provenance as dp
from tests.sentry._ci_lock_scan import ROOT

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_BOOTSTRAP = re.compile(r"EXTRAS=\((--extra \w+(?: --extra \w+)*)\)")
_RELEASE = re.compile(r"^\s*((?:--extra \w+ )+)--python ", re.MULTILINE)
_NEED_UV = "uv is required on every gate host to check the lock against the certified DP rows"


def lock_profile_python() -> str:
    script = ROOT / "scripts/dev/lock_version.sh"
    run = subprocess.run(  # noqa: S603 -- static argv (bash + repo script)
        ["bash", str(script), "--python"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return run.stdout.strip()


def target_env(python_full: str) -> dict[str, str]:
    """Every PEP 508 key, fixed to the certified platform; nothing falls through from the host."""
    return {
        "implementation_name": "cpython",
        "implementation_version": python_full,
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_release": "",
        "platform_system": "Linux",
        "platform_version": "",
        "platform_python_implementation": "CPython",
        "python_version": python_full.rsplit(".", 1)[0],
        "python_full_version": python_full,
        "sys_platform": "linux",
    }


def _names(flags: str) -> list[str]:
    return sorted(flags.replace("--extra ", "").split())


def bootstrap_extras(text: str) -> list[str]:
    line = next(ln for ln in text.splitlines() if "EXTRAS=(" in ln)
    match = _BOOTSTRAP.fullmatch(line)
    assert match, "bootstrap.sh EXTRAS declaration is not exactly --extra <name> items"
    return _names(match.group(1))


def _lock_env_extras(workflow: str, job: str | None) -> list[str]:
    jobs = yaml.safe_load((ROOT / workflow).read_text(encoding="utf-8"))["jobs"]
    steps = jobs[job or next(iter(jobs))]["steps"]
    found = [s["with"]["extras"] for s in steps if "lock-env" in s.get("uses", "")]
    assert len(found) == 1, f"{workflow} must install the certified extras once"
    return sorted(found[0].split())


def other_copies() -> dict[str, list[str]]:
    release = (ROOT / "scripts/release/release_checks.sh").read_text(encoding="utf-8")
    found = _RELEASE.findall(release)
    assert len(found) == 1, "release_checks.sh must declare the certified extras once"
    return {
        "release_checks.sh": _names(found[0]),
        "ci.yml dp-certified-gate": _lock_env_extras(
            ".github/workflows/ci.yml", "dp-certified-gate"
        ),
        "dps-dependency-matrix.yml": _lock_env_extras(
            ".github/workflows/dps-dependency-matrix.yml", None
        ),
    }


def profile_fingerprint(root: Path, extras: list[str], python_full: str, uv: str | None) -> str:
    assert uv, _NEED_UV
    flags = [f for e in extras for f in ("--extra", e)]
    cmd = [uv, "export", "--frozen", "--no-emit-project", "--no-hashes", "--no-header", *flags]
    run = subprocess.run(  # noqa: S603 -- uv from which(), static flags
        cmd, cwd=root, capture_output=True, text=True, check=True
    )
    out = run.stdout
    env = target_env(python_full)
    pins: set[tuple[str, str]] = set()
    for line in out.splitlines():
        if not line or line.startswith((" ", "#")):
            continue
        spec, _, marker = line.partition(" ; ")
        if marker and not Marker(marker).evaluate(env):
            continue
        name, _, version = spec.partition("==")
        pins.add((str(canonicalize_name(name)), version))
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pins.add(("decoy-engine", project["version"]))
    return dp.compute_lock_fingerprint(pins)


def test_lock_profile_is_a_certified_row() -> None:
    py = lock_profile_python()
    extras = bootstrap_extras((ROOT / "scripts/dev/bootstrap.sh").read_text(encoding="utf-8"))
    got = profile_fingerprint(ROOT, extras, py, shutil.which("uv"))
    assert got in dp._CERTIFIED_STACKS[(dp.CERTIFIED_PLATFORM, f"{py}.final")], (
        f"uv.lock profile fingerprint {got} is not a certified row for {py}; re-pin "
        "_CERTIFIED_STACKS (keep released rows) in the same change as the lock"
    )


def test_certified_extras_copies_agree_with_bootstrap() -> None:
    reference = bootstrap_extras((ROOT / "scripts/dev/bootstrap.sh").read_text(encoding="utf-8"))
    assert {k: v for k, v in other_copies().items() if v != reference} == {}


def test_missing_uv_fails_with_the_required_tool_message() -> None:
    with pytest.raises(AssertionError, match="uv is required"):
        profile_fingerprint(ROOT, ["dev"], "3.10.20", None)


@pytest.mark.parametrize("junk", [")x", "; rm x)", " --python 3.10)"])
def test_extras_declaration_with_trailing_junk_is_refused(junk: str) -> None:
    with pytest.raises(AssertionError, match="not exactly"):
        bootstrap_extras(f"EXTRAS=(--extra dev --extra lint{junk}\n")
