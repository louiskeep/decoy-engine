"""R1-c: the CycloneDX SBOM generator (scripts/release/sbom.sh).

Runs the real script on a tiny fixture lock (tests/fixtures/sbom_lock) and checks
the output against the CycloneDX JSON schema, against the lock's own runtime
closure (completeness), for byte-identical reruns, and for leaks of paths or
environment values. Needs ``uv`` and network access to PyPI on first use (the
pinned ``cyclonedx-py`` runs through ``uvx``); marked ``packaging`` like the other
tests that shell out to build tooling.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

pytestmark = pytest.mark.packaging

_REPO_ROOT = Path(__file__).parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "sbom.sh"
_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "sbom_lock"
_REAL_LOCK = _REPO_ROOT / "uv.lock"


def _require_uv() -> None:
    # Fail, do not skip: a gate that silently stops running is worse than a red one.
    assert shutil.which("uv"), "uv is required on PATH to run the SBOM tests"


def _pin(name: str) -> str:
    # The script is the single source of truth for the tool pins.
    text = _SCRIPT.read_text()
    m = re.search(rf'^{name}="?([^"\n]+)"?$', text, re.M)
    assert m, f"{name} pin not found in {_SCRIPT}"
    return m.group(1)


def _run(lock: Path, out: Path, env_extra: dict[str, str] | None = None, cwd: Path | None = None):
    _require_uv()
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(  # noqa: S603 -- static argv (bash + repo script + tmp paths)
        ["bash", str(_SCRIPT), str(lock), str(out)],  # noqa: S607 -- bash resolved from PATH on purpose
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        check=False,
    )


def _generate(tmp_path: Path, name: str = "sbom.cdx.json", **kw) -> Path:
    out = tmp_path / name
    r = _run(_FIXTURE / "uv.lock", out, **kw)
    assert r.returncode == 0, r.stderr
    return out


def _runtime_closure(lock_path: Path) -> dict[tuple[str, str], set[str]]:
    """(name, version) -> artifact sha256 set for the base-profile closure of a uv.lock.

    Independent of uv export / cyclonedx-py: walks ``[[package]].dependencies`` from
    the root project (not optional-dependencies, not dependency-groups).
    """
    data = tomllib.loads(lock_path.read_text())
    pkgs = {p["name"]: p for p in data["package"]}
    roots = [
        p
        for p in data["package"]
        if set(p["source"]) & {"virtual", "editable"}
        and p["source"].get("virtual", p["source"].get("editable")) == "."
    ]
    assert len(roots) == 1
    seen: dict[str, dict] = {}
    stack = [d["name"] for d in roots[0].get("dependencies", [])]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen[n] = pkgs[n]
        stack.extend(d["name"] for d in pkgs[n].get("dependencies", []))
    out: dict[tuple[str, str], set[str]] = {}
    for p in data["package"]:
        if p["name"] not in seen:
            continue
        hashes = set()
        for art in [p.get("sdist", {}), *p.get("wheels", [])]:
            if "hash" in art:
                hashes.add(art["hash"].removeprefix("sha256:"))
        out.setdefault((p["name"], p["version"]), set()).update(hashes)
    return out


def _sbom_components(sbom: dict) -> dict[tuple[str, str], set[str]]:
    comps: dict[tuple[str, str], set[str]] = {}
    for c in sbom["components"]:
        hashes = {h["content"] for h in c.get("hashes", [])}
        for ref in c.get("externalReferences", []):
            hashes |= {h["content"] for h in ref.get("hashes", [])}
        comps.setdefault((c["name"], c["version"]), set()).update(hashes)
    return comps


def missing_from_sbom(lock_path: Path, sbom: dict) -> list[tuple[str, str]]:
    """Locked runtime packages with no component in the SBOM."""
    have = _sbom_components(sbom)
    return sorted(k for k in _runtime_closure(lock_path) if k not in have)


def test_sbom_script_exists():
    assert _SCRIPT.is_file(), "scripts/release/sbom.sh is missing"


def test_sbom_validates_against_cyclonedx_json_schema(tmp_path):
    out = _generate(tmp_path)
    doc = json.loads(out.read_text())
    assert doc["bomFormat"] == "CycloneDX"
    assert tuple(int(x) for x in doc["specVersion"].split(".")) >= (1, 5)
    # Validate with the pinned library's bundled strict JSON schema, in an isolated
    # interpreter, independent of the generator's own --validate step.
    code = (
        "import sys;"
        "from cyclonedx.schema import SchemaVersion;"
        "from cyclonedx.validation.json import JsonStrictValidator;"
        "t=open(sys.argv[1]).read();"
        "v=JsonStrictValidator(SchemaVersion.from_version(sys.argv[2]));"
        "e=v.validate_str(t);"
        "print('INVALID',e) if e else print('VALID');"
        "sys.exit(1 if e else 0)"
    )
    r = subprocess.run(  # noqa: S603 -- static argv (uvx + pinned tool + literals)
        [  # noqa: S607 -- uvx resolved from PATH on purpose
            "uvx",
            "--from",
            f"cyclonedx-bom=={_pin('CYCLONEDX_BOM_VERSION')}",
            "--with",
            f"cyclonedx-python-lib=={_pin('CYCLONEDX_LIB_VERSION')}",
            "python",
            "-I",
            "-c",
            code,
            str(out),
            doc["specVersion"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0 and "VALID" in r.stdout, r.stdout + r.stderr


def test_every_locked_runtime_package_is_in_the_sbom(tmp_path):
    sbom = json.loads(_generate(tmp_path).read_text())
    assert missing_from_sbom(_FIXTURE / "uv.lock", sbom) == []
    have = {k for k in _sbom_components(sbom)}
    # Exactly the base closure: the `dev` extra (iniconfig) must not ship in the SBOM.
    assert have == set(_runtime_closure(_FIXTURE / "uv.lock"))
    assert ("iniconfig", "2.0.0") not in have


def test_completeness_check_reports_a_dropped_package(tmp_path):
    sbom = json.loads(_generate(tmp_path).read_text())
    sbom["components"] = [c for c in sbom["components"] if c["name"] != "six"]
    assert missing_from_sbom(_FIXTURE / "uv.lock", sbom) == [("six", "1.17.0")]


def test_component_hashes_come_from_the_lock(tmp_path):
    sbom = json.loads(_generate(tmp_path).read_text())
    have = _sbom_components(sbom)
    for key, lock_hashes in _runtime_closure(_FIXTURE / "uv.lock").items():
        assert lock_hashes, f"fixture lock has no hashes for {key}"
        assert have[key] == lock_hashes


def test_two_runs_are_byte_identical_even_with_source_date_epoch(tmp_path):
    a = _generate(tmp_path, "a.json", env_extra={"SOURCE_DATE_EPOCH": "1"})
    b = _generate(tmp_path, "b.json", env_extra={"SOURCE_DATE_EPOCH": "1700000000"})
    assert a.read_bytes() == b.read_bytes()
    doc = json.loads(a.read_text())
    assert "serialNumber" not in doc
    assert "timestamp" not in doc["metadata"]


def test_output_contains_no_paths_or_environment_values(tmp_path):
    workdir = tmp_path / "canary-workdir-7731"
    workdir.mkdir()
    canary = "CANARY_ENV_VALUE_9f3a"
    out = tmp_path / "canary-outdir-5520" / "sbom.cdx.json"
    out.parent.mkdir()
    r = _run(
        _FIXTURE / "uv.lock",
        out,
        env_extra={"DECOY_MASK_SECRET": canary, "AWS_SECRET_ACCESS_KEY": canary, "USER": canary},
        cwd=workdir,
    )
    assert r.returncode == 0, r.stderr
    text = out.read_text()
    for needle in (
        canary,
        "canary-workdir",
        "canary-outdir",
        str(tmp_path),
        str(_REPO_ROOT),
        "file://",
    ):
        assert needle not in text
    assert not re.search(r"(?<![\w.:])/(home|tmp|root|Users|var)/", text)


def test_missing_lock_fails_without_writing_output(tmp_path):
    out = tmp_path / "sbom.cdx.json"
    r = _run(tmp_path / "nope" / "uv.lock", out)
    assert r.returncode != 0
    assert "lock file not found" in r.stderr
    assert not out.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda t: t.replace(
                'source = { registry = "https://pypi.org/simple" }',
                'source = { registry = "https://corp-index.internal.example/simple" }',
            ),
            id="private-registry",
        ),
        pytest.param(
            lambda t: t.replace(
                'name = "six"\nversion = "1.17.0"\nsource = { registry = "https://pypi.org/simple" }',
                'name = "six"\nversion = "1.17.0"\nsource = { directory = "../vendored/six" }',
            ),
            id="local-directory",
        ),
    ],
)
def test_non_public_sources_fail_closed(tmp_path, mutate):
    proj = tmp_path / "proj"
    shutil.copytree(_FIXTURE, proj)
    lock = proj / "uv.lock"
    mutated = mutate(lock.read_text())
    assert mutated != lock.read_text()
    lock.write_text(mutated)
    out = tmp_path / "sbom.cdx.json"
    r = _run(lock, out)
    assert r.returncode != 0, "SBOM must refuse locks whose sources are not the public index"
    assert "non-public source" in r.stderr
    assert not out.exists()


def test_real_engine_lock_closure_is_complete(tmp_path):
    out = tmp_path / "sbom.cdx.json"
    r = _run(_REAL_LOCK, out)
    assert r.returncode == 0, r.stderr
    sbom = json.loads(out.read_text())
    assert missing_from_sbom(_REAL_LOCK, sbom) == []


@pytest.mark.parametrize(
    "line",
    [
        "--index-url https://corp-index.internal.example/simple",
        "six @ file:///srv/vendored/six",
        "-e ./local",
    ],
)
def test_unexpected_export_lines_fail_closed(tmp_path, line):
    """Second guard: whatever `uv export` emits must be pins and hashes only."""
    real_uv = shutil.which("uv")
    assert real_uv
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "uv"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = export ]; then\n'
        '  while [ $# -gt 0 ]; do [ "$1" = -o ] && o="$2"; shift; done\n'
        f"  printf '%s\\n' 'six==1.17.0' {line!r} > \"$o\"; exit 0\n"
        "fi\n"
        f'exec {real_uv} "$@"\n'
    )
    shim.chmod(0o755)
    out = tmp_path / "sbom.cdx.json"
    r = _run(
        _FIXTURE / "uv.lock", out, env_extra={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"}
    )
    assert r.returncode != 0
    assert "non-public source or unsupported requirement" in r.stderr
    assert not out.exists()
