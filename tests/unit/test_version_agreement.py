"""Every place the engine states its own version agrees with `[project].version`.

`pyproject.toml` is the one source: it drives the wheel metadata, the lock entry, the SBOM
root component and the release tag (`scripts/release/cut-release.sh` reads the tag name from
it). `decoy_engine.__version__` stays a literal on purpose (it lives in `release.py` and the
root re-exports it): the platform's install gate (`api/engine_compat.py`) compares the imported module's own claim against the dist metadata,
and a module that read its version from that metadata would make the gate vacuous. So the
literal, the lock, the installed metadata, the newest dated CHANGELOG section and the
proof-manifest stamp are held to the pyproject value here instead. The native companion has its own version line; its two
declarations must agree with each other, not with the engine.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import sys
from pathlib import Path

from packaging.version import Version

import decoy_engine

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]

_DATED = re.compile(r"^## \[(?P<v>[^\]]+)\] - (?P<d>\d{4}-\d{2}-\d{2})$", re.M)
_ANY_SECTION = re.compile(r"^## \[(?P<v>[^\]]+)\]", re.M)


def _toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _pyproject_version() -> str:
    project = _toml(ROOT / "pyproject.toml")["project"]
    assert "version" not in project.get("dynamic", []), "the version must stay static"
    return str(project["version"])


def test_pyproject_version_is_canonical_pep440() -> None:
    v = _pyproject_version()
    assert str(Version(v)) == v


def test_module_version_matches_pyproject() -> None:
    assert decoy_engine.__version__ == _pyproject_version()


def test_installed_metadata_matches_pyproject() -> None:
    # A stale editable install (pyproject bumped, env not re-synced) fails here, not in a
    # consumer's startup gate.
    assert importlib.metadata.version("decoy-engine") == _pyproject_version()


def test_lock_root_entry_matches_pyproject() -> None:
    lock = _toml(ROOT / "uv.lock")
    roots = [
        p
        for p in lock["package"]
        if p["name"] == "decoy-engine" and p.get("source", {}).get("editable") == "."
    ]
    assert len(roots) == 1
    assert roots[0]["version"] == _pyproject_version()


def test_newest_changelog_section_is_this_version_and_dated() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = [m.group("v") for m in _ANY_SECTION.finditer(text)]
    assert sections[0] == "Unreleased"
    assert sections[1] == _pyproject_version(), "the newest released section must be this version"
    first_dated = _DATED.search(text)
    assert first_dated is not None
    assert first_dated.group("v") == _pyproject_version()
    assert sections.count(_pyproject_version()) == 1


def test_changelog_released_sections_descend() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = [Version(m.group("v")) for m in _DATED.finditer(text)]
    assert versions == sorted(versions, reverse=True)
    assert len(set(versions)) == len(versions)


def test_native_companion_declarations_agree() -> None:
    native = ROOT / "decoy-engine-native"
    cargo = _toml(native / "Cargo.toml")["package"]["version"]
    pyproj = _toml(native / "pyproject.toml")["project"]["version"]
    assert cargo == pyproj


def test_proof_manifest_stamp_matches_pyproject() -> None:
    manifest = json.loads((ROOT / "docs" / "proof-manifest.json").read_text(encoding="utf-8"))
    assert manifest["engine_version"] == _pyproject_version()
