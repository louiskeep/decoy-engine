"""Model status on the real spaCy model and the real classifier pack."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from decoy_engine.detection import _model_status as ms
from decoy_engine.detection import ner
from decoy_engine.detection.types import DetectorRun, ModelRef
from decoy_engine.profiling.model_pack import _detectors as det
from tests._ml import require_ml_stack
from tests._ner import needs_ner
from tests.unit.detection_verification._fake_ner_model import make_fake_model

PACK = Path(__file__).resolve().parents[3] / "docs" / "v2" / "ml" / "packs" / "lgbm-v1"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DECOY_ML_DISABLED", raising=False)
    ms._CACHE.clear()  # type: ignore[attr-defined]
    det._CACHE.clear()  # type: ignore[attr-defined]


@needs_ner
def test_ner_status_matches_the_installed_model_and_the_pin() -> None:
    import spacy

    status = ms.ner_status()
    assert status.available and status.reason is None
    assert status.source == "ner" and status.model_id == ner.DEFAULT_NER_MODEL
    assert status.version == ner.installed_model_version() == ner.DEFAULT_NER_MODEL_VERSION
    assert status.runtime_version == spacy.__version__


@needs_ner
def test_load_detectors_with_everything_on_the_real_models() -> None:
    require_ml_stack()
    found = det.load_detectors(("ner", "classifier", "regex"), ner_model=ner.DEFAULT_NER_MODEL)
    assert found.runs() == (
        DetectorRun("regex", None),
        DetectorRun("classifier", ModelRef("lgbm-v1", "0.1.0")),
        DetectorRun("ner", ModelRef(ner.DEFAULT_NER_MODEL, ner.DEFAULT_NER_MODEL_VERSION)),
    )
    assert "model.joblib" not in repr(found) and "vec" not in repr(found)


@needs_ner
def test_a_model_built_for_another_spacy_is_refused_naming_versions_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import spacy

    name = make_fake_model(tmp_path, "fake_ner_incompatible", ">=99.0.0,<100.0.0")
    monkeypatch.syspath_prepend(str(tmp_path))
    status = ms.ner_status(name)
    assert (status.available, status.reason) == (False, "provider.ner_model_incompatible")
    with pytest.raises(ner.NerUnavailableError) as caught:
        ner.ensure_ner_available(name)
    assert caught.value.code == "ner_model_incompatible"
    text = str(caught.value)
    assert name in text and "1.2.3" in text and spacy.__version__ in text
    assert ">=99.0.0" not in text  # the model's own range text is not copied into the message
    with pytest.raises(ms.ModelUnavailableError) as refused:
        det.load_detectors(("regex", "ner"), ner_model=name)
    assert refused.value.reason == "provider.ner_model_incompatible"
    assert refused.value.__cause__ is None and refused.value.__context__ is None


@needs_ner
def test_a_compatible_range_is_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    name = make_fake_model(tmp_path, "fake_ner_compatible", ">=3.0.0,<5.0.0")
    monkeypatch.syspath_prepend(str(tmp_path))
    ner.ensure_ner_available(name)  # does not raise
    assert ms.ner_status(name).available


@needs_ner
def test_the_pipeline_cache_is_keyed_by_installed_version_and_findings_name_the_version_that_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ner._PIPELINES.clear()
    first = ner._pipeline(ner.DEFAULT_NER_MODEL)
    monkeypatch.setattr(ner, "installed_model_version", lambda model=ner.DEFAULT_NER_MODEL: "9.9.9")
    second = ner._pipeline(ner.DEFAULT_NER_MODEL)
    assert set(ner._PIPELINES) == {
        (ner.DEFAULT_NER_MODEL, ner.DEFAULT_NER_MODEL_VERSION),
        (ner.DEFAULT_NER_MODEL, "9.9.9"),
    }
    assert first is not second
    # The version a finding records is read from the pipeline that ran, never from the key.
    assert ner.pipeline_version(ner.DEFAULT_NER_MODEL) == second.meta["version"]
    assert ner.pipeline_version(ner.DEFAULT_NER_MODEL) == ner.DEFAULT_NER_MODEL_VERSION


@needs_ner
def test_status_cache_keys_carry_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    ms.ner_status()
    monkeypatch.setattr(ner, "installed_model_version", lambda model=ner.DEFAULT_NER_MODEL: "9.9.9")
    changed = ms.ner_status()
    assert changed.version == "9.9.9"
    keys = set(ms._CACHE)  # type: ignore[attr-defined]
    assert {key[1] for key in keys} == {ner.DEFAULT_NER_MODEL_VERSION, "9.9.9"}
    _ = sys
