"""OSS.3: pin the publishable pyproject.toml metadata for decoy-engine.

Mirror of the test in `decoy/tests/unit/test_pyproject_metadata.py`,
narrowed to the engine's contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

PYPROJECT = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"


def _load() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_distribution_name_is_decoy_engine() -> None:
    """Q-OSS-1 RESOLVED 2026-06-01: engine dist stays `decoy-engine`."""
    data = _load()
    assert data["project"]["name"] == "decoy-engine"


def test_license_is_fsl_1_1_alv2() -> None:
    """Engine and CLI share FSL-1.1-ALv2; each version becomes Apache-2.0 two years after release."""
    data = _load()
    assert data["project"]["license"] == "FSL-1.1-ALv2"
    assert data["project"]["license-files"] == ["LICENSE", "NOTICE"]


def test_classifiers_cover_python_3_10_3_11_3_12() -> None:
    """The engine supports the same Python matrix as the CLI."""
    data = _load()
    classifiers = set(data["project"]["classifiers"])
    for version in ("3.10", "3.11", "3.12"):
        cls = f"Programming Language :: Python :: {version}"
        assert cls in classifiers, f"missing Trove classifier {cls!r}"


def test_classifiers_do_not_claim_osi_approval() -> None:
    """FSL is source-available, not OSI-approved, and has no Trove classifier of its own."""
    data = _load()
    classifiers = data["project"]["classifiers"]
    assert "License :: Other/Proprietary License" in classifiers
    assert not any(c.startswith("License :: OSI Approved") for c in classifiers)


def test_project_urls_present() -> None:
    data = _load()
    urls = data["project"]["urls"]
    for key in ("Homepage", "Repository", "Documentation", "Issues", "Changelog"):
        assert key in urls, f"missing project.urls key {key!r}"


def test_keywords_present() -> None:
    data = _load()
    keywords = data["project"]["keywords"]
    assert isinstance(keywords, list) and len(keywords) >= 5
    assert "decoy" in keywords


def test_native_companion_ships_the_root_license() -> None:
    """The native wheel carries the same LICENSE text as the engine, from its own directory."""
    native = PYPROJECT.parent / "decoy-engine-native"
    with (native / "pyproject.toml").open("rb") as fh:
        project = tomllib.load(fh)["project"]
    assert project["license"] == "FSL-1.1-ALv2"
    assert project["license-files"] == ["LICENSE"]
    assert (native / "LICENSE").read_bytes() == (PYPROJECT.parent / "LICENSE").read_bytes()
