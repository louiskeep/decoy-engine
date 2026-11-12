"""The engine half of "asked-for ML never silently falls back"."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.detection import _model_status as ms
from decoy_engine.detection.ner import DEFAULT_NER_MODEL, NerModelNameError
from decoy_engine.detection.types import DetectorRun, ModelRef
from decoy_engine.profiling.model_pack import _detectors as det
from decoy_engine.profiling.model_pack.loader import ModelPackLoader, ModelPackLoadError

ALL = ("regex", "classifier", "ner")


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DECOY_ML_DISABLED", raising=False)
    ms._CACHE.clear()  # type: ignore[attr-defined]
    det._CACHE.clear()  # type: ignore[attr-defined]


def _runtime_present(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("lightgbm", "sklearn"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["lightgbm"].__version__ = "4.6.0"  # type: ignore[attr-defined]


def _fake_pack() -> dict[str, Any]:
    return {"manifest": SimpleNamespace(pack_id="lgbm-v1", version="0.1.0"), "vec": 1, "clf": 2}


def _refused(sources: tuple[str, ...], code: str, cause: str | None) -> ms.ModelUnavailableError:
    with pytest.raises(ms.ModelUnavailableError) as caught:
        det.load_detectors(sources, ner_model=DEFAULT_NER_MODEL)  # type: ignore[arg-type]
    err = caught.value
    assert err.reason == code
    assert err.cause_class == cause
    assert err.__cause__ is None and err.__context__ is None
    return err


def test_disabled_refuses_and_never_returns_a_regex_only_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DECOY_ML_DISABLED", "1")
    status = det.classifier_status()
    assert (status.available, status.reason, status.cause) == (
        False,
        "provider.classifier_unavailable",
        "disabled",
    )
    err = _refused(("regex", "classifier"), "provider.classifier_unavailable", "disabled")
    assert "model_pack" not in str(err) and "/" not in str(err)


def test_import_failure_is_its_own_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "lightgbm", None)  # `import lightgbm` raises ImportError
    assert det.classifier_status().cause == "import"
    _refused(("regex", "classifier"), "provider.classifier_unavailable", "import")


def test_an_os_error_on_import_is_cause_import(monkeypatch: pytest.MonkeyPatch) -> None:
    # A missing libomp surfaces as OSError from `import lightgbm`, not ImportError.
    real_import = __import__

    def fake_import(name: str, *a: Any, **k: Any) -> Any:
        if name == "lightgbm":
            raise OSError("libomp.dylib not loaded")
        return real_import(name, *a, **k)

    monkeypatch.delitem(sys.modules, "lightgbm", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)
    assert det.classifier_status().cause == "import"


def test_a_bad_pack_is_cause_pack_and_the_loader_message_does_not_cross(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime_present(monkeypatch)

    def bad(self: Any) -> Any:
        raise ModelPackLoadError("SHA mismatch for /secret/path/pack", cause="pack")

    monkeypatch.setattr(ModelPackLoader, "load", bad)
    err = _refused(("regex", "classifier"), "provider.classifier_unavailable", "pack")
    assert "/secret/path" not in str(err) and "/secret/path" not in repr(err.args)


def test_a_lenient_none_after_a_strict_failure_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime_present(monkeypatch)
    monkeypatch.setattr(
        ModelPackLoader, "load", lambda self: (_ for _ in ()).throw(ModelPackLoadError("x"))
    )
    monkeypatch.setattr(ModelPackLoader, "load_with_fallback", lambda self: None)
    _refused(("regex", "classifier"), "provider.classifier_unavailable", "pack")


def test_a_loaded_pack_gives_a_set_with_runs_in_fixed_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime_present(monkeypatch)
    monkeypatch.setattr(ModelPackLoader, "load", lambda self: _fake_pack())
    monkeypatch.setattr(
        ModelPackLoader, "read_manifest", lambda self: _fake_pack()["manifest"], raising=False
    )
    found = det.load_detectors(("classifier", "regex"), ner_model=DEFAULT_NER_MODEL)
    assert found.sources == ("regex", "classifier")
    assert found.classifier_pack is not None
    assert found.runs() == (
        DetectorRun("regex", None),
        DetectorRun("classifier", ModelRef("lgbm-v1", "0.1.0")),
    )
    assert det.load_detectors(("regex",), ner_model=DEFAULT_NER_MODEL).runs() == (
        DetectorRun("regex", None),
    )


def test_missing_spacy_and_missing_model_keep_their_existing_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("decoy_engine.detection.ner.spacy_installed", lambda: False)
    assert ms.ner_status().reason == "provider.ner_spacy_not_installed"
    _refused(("regex", "ner"), "provider.ner_spacy_not_installed", None)
    monkeypatch.setattr("decoy_engine.detection.ner.spacy_installed", lambda: True)
    monkeypatch.setattr("decoy_engine.detection.ner.model_installed", lambda m="": False)
    assert ms.ner_status().reason == "provider.ner_model_not_installed"
    _refused(("regex", "ner"), "provider.ner_model_not_installed", None)


def test_ner_status_never_raises_on_a_hostile_name() -> None:
    status = ms.ner_status("../x")
    assert (status.available, status.reason) == (False, "config.ner_model_invalid")
    assert "../x" not in repr(status)


def test_the_error_is_a_registered_reason_family() -> None:
    from decoy_engine.reason_adapter import reason_from_exception

    record = reason_from_exception(
        ms.ModelUnavailableError(reasons.provider.CLASSIFIER_UNAVAILABLE)
    )
    assert (record.namespace, record.code) == ("provider", "classifier_unavailable")


def test_hostile_ner_model_is_refused_by_load_detectors() -> None:
    with pytest.raises(NerModelNameError) as caught:
        det.load_detectors(("regex", "ner"), ner_model="../x")
    assert caught.value.code == "ner_model_invalid"
    assert "../x" not in str(caught.value)


def test_unknown_source_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        det.load_detectors(("regex", "oracle"), ner_model=DEFAULT_NER_MODEL)  # type: ignore[arg-type]
    _ = Path


def test_the_classifier_cache_is_keyed_by_pack_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _runtime_present(monkeypatch)
    manifest = SimpleNamespace(pack_id="lgbm-v1", version="0.1.0")
    loads: list[str] = []

    def load(self: Any) -> dict[str, Any]:
        loads.append(manifest.version)
        return {"manifest": SimpleNamespace(pack_id="lgbm-v1", version=manifest.version)}

    monkeypatch.setattr(ModelPackLoader, "load", load)
    monkeypatch.setattr(ModelPackLoader, "read_manifest", lambda self: manifest, raising=False)
    first = det.classifier_status()
    assert det.classifier_status() == first and loads == ["0.1.0"]  # a hit, no second load
    manifest.version = "0.2.0"  # a pack upgrade inside a long-lived worker
    upgraded = det.classifier_status()
    assert upgraded.version == "0.2.0" and loads == ["0.1.0", "0.2.0"]
