"""R1-c: the CycloneDX SBOM generator (scripts/release/sbom.sh).

Runs the real script on a tiny fixture lock (tests/fixtures/sbom_lock) and checks
the output against the CycloneDX JSON schema, against the lock's own runtime
closure (completeness), for byte-identical reruns, and for leaks of paths or
environment values. Needs ``uv`` and network access to PyPI on first use (the
pinned ``cyclonedx-py`` runs from a throwaway venv built from sbom-tool-requirements.txt);
marked ``packaging`` like the other tests that shell out to build tooling.
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


_TOOL_REQUIREMENTS = _REPO_ROOT / "scripts" / "release" / "sbom-tool-requirements.txt"


def _pin(name: str) -> str:
    # The hashed tool environment file is the single source of truth for the tool pins.
    m = re.search(rf"^{name}==(\S+)", _TOOL_REQUIREMENTS.read_text(), re.M)
    assert m, f"{name} pin not found in {_TOOL_REQUIREMENTS}"
    return m.group(1)


def _script_copy(tmp_path: Path) -> Path:
    """A throwaway copy of scripts/release so a test can break the tool file safely."""
    dest = tmp_path / "scripts-copy" / "release"
    shutil.copytree(_SCRIPT.parent, dest)
    return dest / "sbom.sh"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _run(
    lock: Path,
    out: Path,
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
    script: Path = _SCRIPT,
):
    _require_uv()
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(  # noqa: S603 -- static argv (bash + repo script + tmp paths)
        ["bash", str(script), str(lock), str(out)],  # noqa: S607 -- bash resolved from PATH on purpose
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
    the root project (not optional-dependencies, not dependency-groups). Packages are
    keyed by (name, version) because one name can be locked at several versions, each
    with its own dependencies; a dependency entry that names a version selects only it.
    """
    data = tomllib.loads(lock_path.read_text())
    by_name: dict[str, list[dict]] = {}
    for p in data["package"]:
        by_name.setdefault(p["name"], []).append(p)
    roots = [
        p
        for p in data["package"]
        if p["source"].get("virtual") == "." or p["source"].get("editable") == "."
    ]
    assert len(roots) == 1
    seen: dict[tuple[str, str], dict] = {}
    stack = list(roots[0].get("dependencies", []))
    while stack:
        dep = stack.pop()
        for p in by_name[dep["name"]]:
            if "version" in dep and p["version"] != dep["version"]:
                continue
            key = (p["name"], p["version"])
            if key not in seen:
                seen[key] = p
                stack.extend(p.get("dependencies", []))
    out: dict[tuple[str, str], set[str]] = {}
    for key, p in seen.items():
        hashes = set()
        for art in [p.get("sdist", {}), *p.get("wheels", [])]:
            if "hash" in art:
                hashes.add(art["hash"].removeprefix("sha256:"))
        out[key] = hashes
    return out


def _sbom_components(sbom: dict) -> dict[tuple[str, str], set[str]]:
    comps: dict[tuple[str, str], set[str]] = {}
    for c in sbom["components"]:
        # PEP 503 names on both sides: typing_extensions and typing-extensions are one package.
        hashes = {h["content"] for h in c.get("hashes", [])}
        for ref in c.get("externalReferences", []):
            hashes |= {h["content"] for h in ref.get("hashes", [])}
        comps.setdefault((_norm(c["name"]), c["version"]), set()).update(hashes)
    return comps


def missing_from_sbom(lock_path: Path, sbom: dict) -> list[tuple[str, str]]:
    """Locked runtime packages with no component in the SBOM."""
    have = _sbom_components(sbom)
    return sorted(k for k in _normalized(_runtime_closure(lock_path)) if k not in have)


def _normalized(closure: dict[tuple[str, str], set[str]]) -> dict[tuple[str, str], set[str]]:
    return {(_norm(n), v): h for (n, v), h in closure.items()}


def test_sbom_script_exists():
    assert _SCRIPT.is_file(), "scripts/release/sbom.sh is missing"


def test_sbom_validates_against_cyclonedx_json_schema(tmp_path):
    out = _generate(tmp_path)
    doc = json.loads(out.read_text())
    assert doc["bomFormat"] == "CycloneDX"
    assert tuple(int(x) for x in doc["specVersion"].split(".")) >= (1, 5)
    # The script's --validate mode: the pinned library's bundled strict JSON schema,
    # in the same hashed tool environment, independent of the generator's own step.
    r = subprocess.run(  # noqa: S603 -- static argv (bash + repo script + tmp path)
        ["bash", str(_SCRIPT), "--validate", str(out)],  # noqa: S607 -- bash from PATH
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0 and "VALID" in r.stdout, r.stdout + r.stderr
    assert _pin("cyclonedx-bom") == "7.6.0"


def test_every_locked_runtime_package_is_in_the_sbom(tmp_path):
    sbom = json.loads(_generate(tmp_path).read_text())
    assert missing_from_sbom(_FIXTURE / "uv.lock", sbom) == []
    have = {k for k in _sbom_components(sbom)}
    # Exactly the base closure: the `dev` extra (iniconfig) must not ship in the SBOM.
    assert have == set(_normalized(_runtime_closure(_FIXTURE / "uv.lock")))
    assert ("iniconfig", "2.0.0") not in have


def test_completeness_check_reports_a_dropped_package(tmp_path):
    sbom = json.loads(_generate(tmp_path).read_text())
    sbom["components"] = [c for c in sbom["components"] if c["name"] != "six"]
    assert missing_from_sbom(_FIXTURE / "uv.lock", sbom) == [("six", "1.17.0")]


def test_component_hashes_come_from_the_lock(tmp_path):
    sbom = json.loads(_generate(tmp_path).read_text())
    have = _sbom_components(sbom)
    for key, lock_hashes in _normalized(_runtime_closure(_FIXTURE / "uv.lock")).items():
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


_H = "--hash=sha256:" + "ab" * 32


def _run_with_export_shim(tmp_path: Path, lines: list[str]):
    real_uv = shutil.which("uv")
    assert real_uv
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    body = tmp_path / "export-body.txt"
    body.write_text("\n".join(lines) + "\n")
    shim = shim_dir / "uv"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = export ]; then\n'
        '  while [ $# -gt 0 ]; do [ "$1" = -o ] && o="$2"; shift; done\n'
        f'  cp {str(body)!r} "$o"; exit 0\n'
        "fi\n"
        f'exec {real_uv} "$@"\n'
    )
    shim.chmod(0o755)
    out = tmp_path / "sbom.cdx.json"
    r = _run(
        _FIXTURE / "uv.lock", out, env_extra={"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"}
    )
    return r, out


def test_well_formed_export_passes_the_shim_harness(tmp_path):
    """Positive control: the shim harness itself does not cause the refusals below."""
    r, out = _run_with_export_shim(tmp_path, ["six==1.17.0 \\", f"    {_H}"])
    assert r.returncode == 0, r.stderr
    assert out.exists()


@pytest.mark.parametrize(
    "lines",
    [
        pytest.param(["--index-url https://corp-index.internal.example/simple"], id="index-url"),
        pytest.param(["six @ file:///srv/vendored/six"], id="file-url"),
        pytest.param(["-e ./local"], id="editable"),
        pytest.param(
            [
                "six==1.17.0 ; python_version >= '3.0' --index-url https://corp.internal.example/s \\",
                f"    {_H}",
            ],
            id="option-after-marker",
        ),
        pytest.param(
            ["six==1.17.0 ; python_version >= '3.0' # https://internal.example/x \\", f"    {_H}"],
            id="comment-after-marker",
        ),
        pytest.param(["six==1.17.0 \\", "    --hash=sha256:xyz"], id="malformed-hash"),
        pytest.param(["six==1.17.0"], id="pin-without-hash"),
        pytest.param(["six==1.17.0,--no-binary=six \\", f"    {_H}"], id="version-token-smuggling"),
        pytest.param(["six==1.17.0,!=1.0 \\", f"    {_H}"], id="version-specifier-list"),
        pytest.param([f"    {_H}"], id="orphan-hash"),
        pytest.param(["six==1.17.0 \\"], id="dangling-continuation"),
        pytest.param(["six==1.17.0", f"    {_H}"], id="hash-after-unterminated-pin"),
    ],
)
def test_unexpected_export_lines_fail_closed(tmp_path, lines):
    """Whatever `uv export` emits must be hashed pins only, as a well-formed sequence."""
    r, out = _run_with_export_shim(tmp_path, lines)
    assert r.returncode != 0
    assert "non-public source or unsupported requirement" in r.stderr
    assert not out.exists()


# --- hashed tool environment ------------------------------------------------


def test_tool_requirements_file_is_fully_hashed_and_pinned():
    entries = re.findall(r"^([A-Za-z0-9._-]+)==\S+", _TOOL_REQUIREMENTS.read_text(), re.M)
    assert {"cyclonedx-bom", "cyclonedx-python-lib"} <= set(entries)
    # every requirement block carries at least one sha256
    blocks = re.split(r"(?m)^(?=[A-Za-z0-9._-]+==)", _TOOL_REQUIREMENTS.read_text())[1:]
    assert blocks
    assert all("--hash=sha256:" in b for b in blocks)


def test_missing_tool_requirements_fails_without_writing_output(tmp_path):
    script = _script_copy(tmp_path)
    (script.parent / "sbom-tool-requirements.txt").unlink()
    out = tmp_path / "sbom.cdx.json"
    r = _run(_FIXTURE / "uv.lock", out, script=script)
    assert r.returncode != 0
    assert "tool requirements file not found" in r.stderr
    assert not out.exists()


def test_tool_hash_mismatch_fails_without_writing_output(tmp_path):
    script = _script_copy(tmp_path)
    req = script.parent / "sbom-tool-requirements.txt"
    text = req.read_text()
    i = text.index("arrow==")
    j = text.index("\nattrs==")
    # corrupt every hash of one package (one good hash would still satisfy the check)
    block = re.sub(r"sha256:[0-9a-f]{4}", "sha256:0000", text[i:j])
    req.write_text(text[:i] + block + text[j:])
    out = tmp_path / "sbom.cdx.json"
    r = _run(_FIXTURE / "uv.lock", out, script=script)
    assert r.returncode != 0
    assert "tool environment install failed" in r.stderr
    assert not out.exists()


def test_tool_requirement_without_a_hash_fails(tmp_path):
    """--require-hashes: one unhashed entry must refuse the whole install."""
    script = _script_copy(tmp_path)
    req = script.parent / "sbom-tool-requirements.txt"
    text = req.read_text()
    i = text.index("arrow==")
    j = text.index("\nattrs==")
    block = re.sub(r" \\\n\s+--hash=sha256:[0-9a-f]{64}", "", text[i:j])
    req.write_text(text[:i] + block + text[j:])
    out = tmp_path / "sbom.cdx.json"
    r = _run(_FIXTURE / "uv.lock", out, script=script)
    assert r.returncode != 0
    assert "tool environment install failed" in r.stderr
    assert not out.exists()


# --- runtime-closure scoped source rule ---------------------------------------


def _load_checks():
    import importlib.util

    spec = importlib.util.spec_from_file_location("sbom_checks", _SCRIPT.parent / "sbom_checks.py")
    assert spec and spec.loader and (_SCRIPT.parent / "sbom_checks.py").is_file()
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_MULTI = _REPO_ROOT / "tests" / "fixtures" / "sbom_multiversion" / "uv.lock"


def test_closure_oracle_keeps_dependencies_of_every_locked_version():
    closure = _runtime_closure(_MULTI)
    assert set(closure) == {("multi", "1.0"), ("multi", "2.0"), ("leaf", "1.0")}
    assert "devtool" not in {n for n, _ in closure}


def test_checker_closure_matches_oracle_on_multi_version_lock():
    checks = _load_checks()
    got = checks.runtime_closure(tomllib.loads(_MULTI.read_text()))
    assert got == set(_runtime_closure(_MULTI))


def test_checker_ignores_non_public_source_outside_the_runtime_closure():
    checks = _load_checks()
    # devtool is a git source, but only in the dev group.
    assert checks.lock_violations(_MULTI.read_text()) == []


def test_checker_rejects_non_public_source_inside_the_runtime_closure():
    checks = _load_checks()
    text = _MULTI.read_text().replace(
        'name = "leaf"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }',
        'name = "leaf"\nversion = "1.0"\nsource = { git = "https://example.invalid/leaf.git" }',
    )
    assert checks.lock_violations(text) == ["leaf 1.0"]


def test_checker_scopes_by_version_not_by_name():
    checks = _load_checks()
    # multi 1.0 (which pulls the git leaf) is not required when the root asks for 2.0 only.
    text = (
        _MULTI.read_text()
        .replace(
            'name = "leaf"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }',
            'name = "leaf"\nversion = "1.0"\nsource = { git = "https://example.invalid/leaf.git" }',
        )
        .replace(
            '    { name = "multi", version = "1.0", source = { registry = "https://pypi.org/simple" }, marker = "python_full_version < \'3.11\'" },\n',
            "",
        )
    )
    assert checks.lock_violations(text) == []


def test_dev_group_with_a_non_registry_source_still_produces_an_sbom(tmp_path):
    proj = tmp_path / "proj"
    (proj / "devpkg").mkdir(parents=True)
    (proj / "devpkg" / "pyproject.toml").write_text(
        '[project]\nname = "devpkg"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    )
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "sbom-fixture-dev"\nversion = "1.0.0"\nrequires-python = ">=3.10"\n'
        'license = "Apache-2.0"\ndependencies = ["six==1.17.0"]\n'
        '[dependency-groups]\ndev = ["devpkg"]\n'
        '[tool.uv.sources]\ndevpkg = { path = "devpkg", editable = true }\n'
    )
    r = subprocess.run(  # noqa: S603 -- static argv
        ["uv", "lock", "--project", str(proj)],  # noqa: S607 -- uv from PATH
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "editable" in (proj / "uv.lock").read_text()
    out = tmp_path / "sbom.cdx.json"
    r = _run(proj / "uv.lock", out)
    assert r.returncode == 0, r.stderr
    names = {c["name"] for c in json.loads(out.read_text())["components"]}
    assert names == {"six"}


def test_runtime_package_with_a_git_source_fails(tmp_path):
    proj = tmp_path / "proj"
    shutil.copytree(_FIXTURE, proj)
    lock = proj / "uv.lock"
    text = lock.read_text()
    mutated = text.replace(
        'name = "six"\nversion = "1.17.0"\nsource = { registry = "https://pypi.org/simple" }',
        'name = "six"\nversion = "1.17.0"\nsource = { git = "https://example.invalid/six.git?rev=abc#abc" }',
    )
    assert mutated != text
    lock.write_text(mutated)
    out = tmp_path / "sbom.cdx.json"
    r = _run(lock, out)
    assert r.returncode != 0
    assert "non-public source" in r.stderr
    assert "example.invalid" not in r.stderr
    assert not out.exists()


# --- messages, staleness, names, purls ----------------------------------------


def test_error_messages_do_not_echo_the_callers_path(tmp_path):
    secret_dir = tmp_path / "canary-private-dir-4417"
    out = tmp_path / "sbom.cdx.json"
    r = _run(secret_dir / "uv.lock", out)
    assert r.returncode != 0
    assert "canary-private-dir" not in r.stderr
    wrong = tmp_path / "canary-wrong-name-9921"
    wrong.mkdir()
    (wrong / "other.lock").write_text("")
    r = _run(wrong / "other.lock", out)
    assert r.returncode != 0
    assert "canary-wrong-name" not in r.stderr


def test_lock_stale_against_pyproject_fails_without_writing_output(tmp_path):
    proj = tmp_path / "proj"
    shutil.copytree(_FIXTURE, proj)
    pyproject = proj / "pyproject.toml"
    text = pyproject.read_text()
    pyproject.write_text(
        text.replace(
            'dependencies = ["python-dateutil==2.9.0.post0"]',
            'dependencies = ["python-dateutil==2.9.0.post0", "six==1.17.0"]',
        )
    )
    assert pyproject.read_text() != text
    out = tmp_path / "sbom.cdx.json"
    r = _run(proj / "uv.lock", out)
    assert r.returncode != 0
    assert "uv export failed" in r.stderr
    assert not out.exists()


def test_completeness_check_normalizes_package_names():
    sbom = {
        "components": [{"name": "typing_extensions", "version": "4.0.0"}],
    }
    lock = _REPO_ROOT / "tests" / "fixtures" / "sbom_multiversion" / "uv.lock"
    # multi/leaf are absent -> reported; the name normalizer itself is exercised directly:
    assert _norm("typing_extensions") == _norm("Typing-Extensions") == "typing-extensions"
    assert ("typing-extensions", "4.0.0") in _sbom_components(sbom)
    assert missing_from_sbom(lock, sbom) == [("leaf", "1.0"), ("multi", "1.0"), ("multi", "2.0")]


def test_every_component_has_a_pypi_purl_matching_its_name_and_version(tmp_path):
    sbom = json.loads(_generate(tmp_path).read_text())
    assert sbom["components"]
    for c in sbom["components"]:
        assert c["purl"] == f"pkg:pypi/{_norm(c['name'])}@{c['version']}"


# --- round 2: run-time isolation (class A) -----------------------------------


def _shadow_dir(tmp_path: Path) -> Path:
    shadow = tmp_path / "shadow"
    (shadow / "cyclonedx_py").mkdir(parents=True)
    (shadow / "cyclonedx_py" / "__init__.py").write_text('raise SystemExit("SHADOWED_TOOL")\n')
    return shadow


def test_pythonpath_cannot_shadow_the_hashed_tool(tmp_path):
    out = tmp_path / "sbom.cdx.json"
    r = _run(_FIXTURE / "uv.lock", out, env_extra={"PYTHONPATH": str(_shadow_dir(tmp_path))})
    assert r.returncode == 0, r.stderr
    assert "SHADOWED_TOOL" not in r.stderr
    assert out.is_file()


def _canary_uv_config(tmp_path: Path) -> dict[str, str]:
    """A user uv.toml and a cwd uv.toml that both point uv at an unreachable index."""
    cfg = '[pip]\nindex-url = "https://canary-index.invalid/simple"\n'
    xdg = tmp_path / "xdg"
    (xdg / "uv").mkdir(parents=True)
    (xdg / "uv" / "uv.toml").write_text(cfg)
    (tmp_path / "uv.toml").write_text(cfg)
    return {"XDG_CONFIG_HOME": str(xdg)}


def test_ambient_uv_config_files_cannot_change_the_tool_index(tmp_path):
    out = tmp_path / "sbom.cdx.json"
    r = _run(_FIXTURE / "uv.lock", out, env_extra=_canary_uv_config(tmp_path), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert out.is_file()


@pytest.mark.parametrize("var", ["UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL"])
def test_ambient_uv_environment_cannot_change_the_tool_index(tmp_path, var):
    out = tmp_path / "sbom.cdx.json"
    r = _run(_FIXTURE / "uv.lock", out, env_extra={var: "https://canary-index.invalid/simple"})
    assert r.returncode == 0, r.stderr
    assert out.is_file()


def test_ambient_uv_environment_cannot_change_the_export(tmp_path):
    """UV_FROZEN would skip the --locked staleness check; the scrub must drop it."""
    proj = tmp_path / "proj"
    shutil.copytree(_FIXTURE, proj)
    pyproject = proj / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            'dependencies = ["python-dateutil==2.9.0.post0"]',
            'dependencies = ["python-dateutil==2.9.0.post0", "six==1.17.0"]',
        )
    )
    out = tmp_path / "sbom.cdx.json"
    r = _run(proj / "uv.lock", out, env_extra={"UV_FROZEN": "1"})
    assert r.returncode != 0
    assert "uv export failed" in r.stderr
    assert not out.exists()


def _uv_shim(tmp_path: Path, venv_python: str) -> dict[str, str]:
    """PATH shim: `uv venv` gets a different interpreter, everything else is real uv."""
    real_uv = shutil.which("uv")
    assert real_uv
    shim_dir = tmp_path / "uv-shim"
    shim_dir.mkdir()
    shim = shim_dir / "uv"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = venv ]; then\n'
        "  args=()\n"
        '  for a in "$@"; do\n'
        f'    if [ "${{prev:-}}" = --python ]; then a={venv_python}; fi\n'
        '    args+=("$a"); prev=$a\n'
        "  done\n"
        f'  exec {real_uv} "${{args[@]}}"\n'
        "fi\n"
        f'exec {real_uv} "$@"\n'
    )
    shim.chmod(0o755)
    return {"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"}


def test_tool_interpreter_other_than_the_locked_minor_is_refused(tmp_path):
    out = tmp_path / "sbom.cdx.json"
    r = _run(_FIXTURE / "uv.lock", out, env_extra=_uv_shim(tmp_path, "3.13"))
    assert r.returncode != 0
    assert "tool interpreter is not CPython 3.12" in r.stderr
    assert not out.exists()


# docopt 0.6.2 is published as an sdist only.
_SDIST_ONLY = (
    "docopt==0.6.2 \\\n"
    "    --hash=sha256:49b3a825280bd66b3aa83585ef59c4a8c82f2c8a522dbe754a8bc8d08c85c491\n"
)


def test_tool_install_never_builds_from_source(tmp_path):
    script = _script_copy(tmp_path)
    req = script.parent / "sbom-tool-requirements.txt"
    req.write_text(req.read_text() + _SDIST_ONLY)
    out = tmp_path / "sbom.cdx.json"
    r = _run(_FIXTURE / "uv.lock", out, script=script)
    assert r.returncode != 0
    assert "tool environment install failed" in r.stderr
    assert not out.exists()


_SHA = "ab" * 32
_BAD_TOOL_LINES = {
    "index-url": "--index-url https://canary-index.invalid/simple\n",
    "extra-index-url": "--extra-index-url https://canary-index.invalid/simple\n",
    "find-links": "--find-links ./wheels\n",
    "trusted-host": "--trusted-host canary-index.invalid\n",
    "direct-url": f"six @ https://canary-index.invalid/six.whl \\\n    --hash=sha256:{_SHA}\n",
    "path": f"./vendored/six \\\n    --hash=sha256:{_SHA}\n",
    "unhashed-pin": "six==1.17.0\n",
    "non-pep440-version": f"six==1..0 \\\n    --hash=sha256:{_SHA}\n",
    "cr-in-comment": "# via\rsix==1.17.0\n",
}


@pytest.mark.parametrize("line", list(_BAD_TOOL_LINES.values()), ids=list(_BAD_TOOL_LINES))
def test_tool_requirements_grammar_is_checked_before_install(tmp_path, line):
    script = _script_copy(tmp_path)
    req = script.parent / "sbom-tool-requirements.txt"
    req.write_bytes((req.read_text() + line).encode())
    out = tmp_path / "sbom.cdx.json"
    r = _run(_FIXTURE / "uv.lock", out, script=script)
    assert r.returncode != 0
    assert "sbom-tool-requirements.txt is not hashed pins only" in r.stderr
    assert "canary-index" not in r.stderr
    assert not out.exists()


def _run_lock(script: Path, env_extra: dict[str, str] | None = None, cwd: Path | None = None):
    _require_uv()
    return subprocess.run(  # noqa: S603 -- static argv (bash + copied repo script)
        ["bash", str(script)],  # noqa: S607 -- bash from PATH
        capture_output=True,
        text=True,
        env={**os.environ, **(env_extra or {})},
        cwd=cwd,
        check=False,
    )


def test_tool_lock_ignores_ambient_uv_config_and_environment(tmp_path):
    script = _script_copy(tmp_path).parent / "sbom-tool-lock.sh"
    env = {**_canary_uv_config(tmp_path), "UV_INDEX_URL": "https://canary-index.invalid/simple"}
    r = _run_lock(script, env_extra=env, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (script.parent / "sbom-tool-requirements.txt").read_bytes() == (
        _TOOL_REQUIREMENTS.read_bytes()
    )


def test_tool_lock_refuses_a_dependency_without_wheels(tmp_path):
    script = _script_copy(tmp_path).parent / "sbom-tool-lock.sh"
    req_in = script.parent / "sbom-tool-requirements.in"
    req_in.write_text(req_in.read_text() + "docopt==0.6.2\n")
    r = _run_lock(script)
    assert r.returncode != 0
    assert "docopt" not in (script.parent / "sbom-tool-requirements.txt").read_text()


def test_cdpath_does_not_redirect_or_pollute_the_script_directory(tmp_path):
    decoy = tmp_path / "decoy-tree"
    (decoy / "scripts" / "release").mkdir(parents=True)
    out = tmp_path / "sbom.cdx.json"
    r = subprocess.run(  # noqa: S603 -- static argv
        ["bash", "scripts/release/sbom.sh", str(_FIXTURE / "uv.lock"), str(out)],  # noqa: S607
        capture_output=True,
        text=True,
        env={**os.environ, "CDPATH": str(decoy)},
        cwd=_REPO_ROOT,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert out.is_file()


def test_extra_positional_arguments_are_refused(tmp_path):
    _require_uv()
    out = tmp_path / "sbom.cdx.json"
    r = subprocess.run(  # noqa: S603 -- static argv
        ["bash", str(_SCRIPT), str(_FIXTURE / "uv.lock"), str(out), "surplus"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode != 0
    assert "usage:" in r.stderr
    assert not out.exists()


@pytest.mark.parametrize("extra", [[], ["a.json", "b.json"]], ids=["no-file", "two-files"])
def test_validate_needs_exactly_one_file(extra):
    _require_uv()
    r = subprocess.run(  # noqa: S603 -- static argv
        ["bash", str(_SCRIPT), "--validate", *extra],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode != 0
    assert "usage:" in r.stderr


# --- round 2: export checker versus parser (class B) ---------------------------

_GRAMMAR = "non-public source or unsupported requirement"
_SEPARATORS = ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


@pytest.mark.parametrize("sep", _SEPARATORS, ids=[f"U+{ord(s):04X}" for s in _SEPARATORS])
def test_splitlines_separators_in_the_export_are_refused(tmp_path, sep):
    r, out = _run_with_export_shim(tmp_path, ["six==1.17.0 \\", f"    {_H}", sep])
    assert r.returncode != 0
    assert _GRAMMAR in r.stderr
    assert not out.exists()


def test_crlf_export_is_refused(tmp_path):
    r, out = _run_with_export_shim(tmp_path, ["six==1.17.0 \\\r", f"    {_H}\r"])
    assert r.returncode != 0
    assert _GRAMMAR in r.stderr
    assert not out.exists()


@pytest.mark.parametrize("version", ["1..0", "1.17.0-", "1.17.0_post1", ".1", "1.0+Local", "01.0"])
def test_export_versions_must_be_canonical_pep440(tmp_path, version):
    r, out = _run_with_export_shim(tmp_path, [f"six=={version} \\", f"    {_H}"])
    assert r.returncode != 0
    assert _GRAMMAR in r.stderr
    assert not out.exists()


@pytest.mark.parametrize(
    "marker",
    ["python_version >>= '3.0'", "platform_cpu == 'x86'", "(python_version >= '3.0'"],
    ids=["bad-operator", "unknown-variable", "unbalanced"],
)
def test_export_pins_the_requirement_parser_rejects_are_refused(tmp_path, marker):
    r, out = _run_with_export_shim(tmp_path, [f"six==1.17.0 ; {marker} \\", f"    {_H}"])
    assert r.returncode != 0
    assert _GRAMMAR in r.stderr
    assert not out.exists()


def test_exported_package_outside_the_checked_closure_is_refused(tmp_path):
    # iniconfig is locked, but only for the `dev` extra: its source was never checked.
    r, out = _run_with_export_shim(
        tmp_path, ["six==1.17.0 \\", f"    {_H}", "iniconfig==2.0.0 \\", f"    {_H}"]
    )
    assert r.returncode != 0
    assert "SBOM does not match the runtime closure" in r.stderr
    assert not out.exists()


def test_component_count_equals_export_pin_count(tmp_path):
    sbom = json.loads(_generate(tmp_path).read_text())
    closure = _normalized(_runtime_closure(_FIXTURE / "uv.lock"))
    assert len(sbom["components"]) == len(closure)
    assert sorted((_norm(c["name"]), c["version"]) for c in sbom["components"]) == sorted(closure)


def test_completeness_check_flags_dropped_merged_or_added_components():
    checks = _load_checks()
    export = f"six==1.17.0 \\\n    {_H}\nleaf==1.0 \\\n    {_H}\n"
    six = {"name": "six", "version": "1.17.0"}
    leaf = {"name": "Leaf", "version": "1.0.0"}
    root = {"metadata": {"component": {"name": "root"}}}
    assert checks.completeness_violations(export, {**root, "components": [six, leaf]}) == []
    assert checks.completeness_violations(export, {**root, "components": [six]}) == ["leaf 1.0"]
    assert checks.completeness_violations(export, {**root, "components": [six, six, leaf]})
    assert checks.completeness_violations(export, {"components": [six, leaf]})


def test_pin_violations_treats_crlf_like_the_script_path():
    checks = _load_checks()
    assert checks.pin_violations(f"six==1.17.0 \\\r\n    {_H}\r\n", require_hashes=True)
    assert checks.pin_violations(f"six==1.17.0 \\\n    {_H}\n", require_hashes=True) == []


# --- round 2: source-keyed closure (item 5) ------------------------------------

_GIT = '{ git = "https://example.invalid/six.git?rev=abc#abc" }'
_PYPI = '{ registry = "https://pypi.org/simple" }'


def _twin_lock(edge_source: str | None, git_first: bool, *, dev_edge: str | None = None) -> str:
    """Two `six 1.17.0` entries (PyPI and git); the runtime edge names one by source."""
    pypi = f'[[package]]\nname = "six"\nversion = "1.17.0"\nsource = {_PYPI}\n'
    git = f'[[package]]\nname = "six"\nversion = "1.17.0"\nsource = {_GIT}\n'
    twins = git + "\n" + pypi if git_first else pypi + "\n" + git
    dev = (
        f'\n[package.dev-dependencies]\ndev = [\n    {{ name = "six", version = "1.17.0", source = {dev_edge} }},\n]\n'
        if dev_edge
        else ""
    )
    return (
        'version = 1\nrevision = 3\nrequires-python = ">=3.10"\n\n'
        f'[[package]]\nname = "dateutil-ish"\nversion = "1.0"\nsource = {_PYPI}\n'
        + (
            f'dependencies = [\n    {{ name = "six", version = "1.17.0", source = {edge_source} }},\n]\n\n'
            if edge_source
            else 'dependencies = [\n    { name = "six", version = "1.17.0" },\n]\n\n'
        )
        + f"{twins}\n"
        '[[package]]\nname = "root"\nversion = "1.0.0"\nsource = { virtual = "." }\n'
        'dependencies = [\n    { name = "dateutil-ish" },\n]\n'
        f"{dev}"
    )


@pytest.mark.parametrize("git_first", [False, True], ids=["pypi-first", "git-first"])
def test_runtime_edge_to_the_git_twin_is_refused_in_either_order(git_first):
    checks = _load_checks()
    assert checks.lock_violations(_twin_lock(_GIT, git_first)) == ["six 1.17.0"]


@pytest.mark.parametrize("git_first", [False, True], ids=["pypi-first", "git-first"])
def test_dev_only_git_twin_does_not_hide_or_taint_the_public_one(git_first):
    checks = _load_checks()
    assert checks.lock_violations(_twin_lock(_PYPI, git_first, dev_edge=_GIT)) == []


def test_closure_follows_requested_extras():
    checks = _load_checks()
    text = (
        "version = 1\nrevision = 3\n\n"
        f'[[package]]\nname = "bar"\nversion = "1.0"\nsource = {_GIT}\n\n'
        f'[[package]]\nname = "foo"\nversion = "1.0"\nsource = {_PYPI}\n\n'
        '[package.optional-dependencies]\nx = [\n    { name = "bar" },\n]\n\n'
        '[[package]]\nname = "root"\nversion = "1.0.0"\nsource = { virtual = "." }\n'
        'dependencies = [\n    { name = "foo", extra = ["x"] },\n]\n'
    )
    assert checks.lock_violations(text) == ["bar 1.0"]


def test_versionless_runtime_package_gets_the_non_public_source_refusal(tmp_path, capsys):
    checks = _load_checks()
    lock = tmp_path / "uv.lock"
    lock.write_text(
        (_FIXTURE / "uv.lock")
        .read_text()
        .replace(
            f'name = "six"\nversion = "1.17.0"\nsource = {_PYPI}',
            f'name = "six"\nsource = {_GIT}',
        )
    )
    assert 'name = "six"\nsource' in lock.read_text()
    assert checks.main(["sbom_checks.py", "lock", str(lock)]) == 1
    err = capsys.readouterr().err
    assert "non-public source" in err
    assert "no single root project" not in err
    assert "example.invalid" not in err


def test_project_uv_config_cannot_change_the_export(tmp_path):
    baseline = _generate(tmp_path, "baseline.json")
    proj = tmp_path / "proj"
    shutil.copytree(_FIXTURE, proj)
    (proj / "uv.toml").write_text('exclude-newer = "2020-01-01T00:00:00Z"\n')
    out = tmp_path / "sbom.cdx.json"
    r = _run(proj / "uv.lock", out)
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() == baseline.read_bytes()


@pytest.mark.parametrize("sep", _SEPARATORS, ids=[f"U+{ord(s):04X}" for s in _SEPARATORS])
def test_splitlines_separators_in_a_tool_file_comment_are_refused(tmp_path, sep):
    # Comments are allowed in the tool file, so only the printable-ASCII rule stops a
    # separator from smuggling a pin past the checker.
    script = _script_copy(tmp_path)
    req = script.parent / "sbom-tool-requirements.txt"
    req.write_bytes((req.read_text() + f"# via{sep}six==1.17.0\n").encode())
    out = tmp_path / "sbom.cdx.json"
    r = _run(_FIXTURE / "uv.lock", out, script=script)
    assert r.returncode != 0
    assert "sbom-tool-requirements.txt is not hashed pins only" in r.stderr
    assert not out.exists()


@pytest.mark.parametrize("git_first", [False, True], ids=["pypi-first", "git-first"])
def test_sourceless_edge_reaching_both_twins_keeps_both_in_either_order(git_first):
    checks = _load_checks()
    assert checks.lock_violations(_twin_lock(None, git_first)) == ["six 1.17.0"]
    assert len(checks._closure_packages(tomllib.loads(_twin_lock(None, git_first)))) == 3
