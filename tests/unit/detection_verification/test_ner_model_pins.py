"""The pinned NER model wheels and the ner extra stay well-formed. No spaCy needed."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from decoy_engine.detection.ner import DEFAULT_NER_MODEL, NER_MODEL_WHEELS

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[3]
RELEASES = "https://github.com/explosion/spacy-models/releases/download/"


def test_default_model_is_pinned() -> None:
    assert DEFAULT_NER_MODEL in NER_MODEL_WHEELS


@pytest.mark.parametrize("model", sorted(NER_MODEL_WHEELS))
def test_wheel_pin_is_https_release_asset_with_sha256(model: str) -> None:
    wheel = NER_MODEL_WHEELS[model]
    assert re.fullmatch(r"[0-9a-f]{64}", wheel.sha256)
    assert wheel.url.startswith(RELEASES)
    tag, _, filename = wheel.url[len(RELEASES) :].partition("/")
    m = re.fullmatch(rf"{model}-(\d+\.\d+\.\d+)", tag)
    assert m, f"release tag {tag!r} does not name {model}"
    assert filename == f"{model}-{m.group(1)}-py3-none-any.whl"


@pytest.mark.parametrize("model", sorted(NER_MODEL_WHEELS))
def test_model_minor_matches_locked_spacy(model: str) -> None:
    """spaCy models declare spacy>=X.Y,<X.(Y+1); a lock bump past that breaks the load."""
    wheel = NER_MODEL_WHEELS[model]
    model_minor = re.search(r"-(\d+\.\d+)\.\d+/", wheel.url)
    assert model_minor
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    (spacy,) = [p for p in lock["package"] if p["name"] == "spacy"]
    assert spacy["version"].startswith(model_minor.group(1) + ".")


def test_ner_extra_declares_click() -> None:
    """spaCy imports click but typer no longer pulls it in, so the extra must."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extra = data["project"]["optional-dependencies"]["ner"]
    assert any(re.match(r"click\b", req) for req in extra), extra
    assert any(re.match(r"spacy\b", req) for req in extra), extra
