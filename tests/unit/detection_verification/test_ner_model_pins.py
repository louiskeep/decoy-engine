"""The pinned NER model wheels and the ner extra stay well-formed. No spaCy needed."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from decoy_engine.detection.ner import (
    DEFAULT_NER_MODEL,
    DEFAULT_NER_MODEL_VERSION,
    NER_MODEL_WHEELS,
)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[3]
RELEASES = "https://github.com/explosion/spacy-models/releases/download/"
KEYS = sorted(NER_MODEL_WHEELS)


def test_default_model_is_pinned() -> None:
    assert (DEFAULT_NER_MODEL, DEFAULT_NER_MODEL_VERSION) in NER_MODEL_WHEELS


@pytest.mark.parametrize(("model", "version"), KEYS)
def test_wheel_pin_is_https_release_asset_with_sha256(model: str, version: str) -> None:
    wheel = NER_MODEL_WHEELS[(model, version)]
    assert re.fullmatch(r"[0-9a-f]{64}", wheel.sha256)
    assert wheel.url == f"{RELEASES}{model}-{version}/{model}-{version}-py3-none-any.whl"


@pytest.mark.parametrize(("model", "version"), KEYS)
def test_locked_spacy_is_inside_the_model_range(model: str, version: str) -> None:
    """A model refuses to load under a spaCy outside its meta.json range."""
    wheel = NER_MODEL_WHEELS[(model, version)]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (spacy,) = [p for p in lock["package"] if p["name"] == "spacy"]
    assert Version(spacy["version"]) in SpecifierSet(wheel.spacy_range)


def test_ner_extra_declares_click() -> None:
    """spaCy imports click but typer no longer pulls it in, so the extra must."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extra = data["project"]["optional-dependencies"]["ner"]
    assert any(re.match(r"click\b", req) for req in extra), extra
    assert any(re.match(r"spacy\b", req) for req in extra), extra
