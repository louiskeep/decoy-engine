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


def test_license_is_apache_2_0() -> None:
    """The engine ships Apache-2.0 from day one (unlike the CLI dist,
    which keeps BUSL-1.1 until OSS.2 flips it)."""
    data = _load()
    assert data["project"]["license"] == "Apache-2.0"


def test_classifiers_cover_python_3_10_3_11_3_12() -> None:
    """The engine supports the same Python matrix as the CLI."""
    data = _load()
    classifiers = set(data["project"]["classifiers"])
    for version in ("3.10", "3.11", "3.12"):
        cls = f"Programming Language :: Python :: {version}"
        assert cls in classifiers, f"missing Trove classifier {cls!r}"


def test_classifiers_include_apache_license() -> None:
    """The OSI-Approved Apache classifier is what makes the PyPI license
    filter resolve correctly."""
    data = _load()
    classifiers = data["project"]["classifiers"]
    assert "License :: OSI Approved :: Apache Software License" in classifiers


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
