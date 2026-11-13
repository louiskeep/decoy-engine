"""Classifier status on the real shipped pack. Needs only the base classifier stack, so `ml-gate` runs it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import decoy_engine
from tests._ml import require_ml_stack

pytestmark = pytest.mark.ml

require_ml_stack()

import lightgbm  # noqa: E402

PACK = Path(__file__).resolve().parents[3] / "docs" / "v2" / "ml" / "packs" / "lgbm-v1"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DECOY_ML_DISABLED", raising=False)


def test_classifier_status_matches_the_manifest() -> None:
    manifest = json.loads((PACK / "manifest.json").read_text())
    status = decoy_engine.classifier_status()
    assert status.available and status.reason is None and status.cause is None
    assert (status.model_id, status.version) == (manifest["pack_id"], manifest["version"])
    assert status.runtime_version == lightgbm.__version__


def test_load_detectors_returns_the_strictly_loaded_pack() -> None:
    found = decoy_engine.load_detectors(("regex", "classifier"), ner_model="en_core_web_sm")
    assert found.classifier_pack is not None
    assert [(r.source, r.model and (r.model.id, r.model.version)) for r in found.runs()] == [
        ("regex", None),
        ("classifier", ("lgbm-v1", "0.1.0")),
    ]
