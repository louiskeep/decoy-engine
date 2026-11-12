"""Strict NER availability: a name is a model only if it is an installed model distribution.

`find_spec` finds every importable module, so `ner_status("os")` used to count as available and
`spacy.util.get_package_path` then imported whatever the name said. A model is now an installed
distribution with a version and a meta.json, found without importing anything, and a spaCy that
is installed but will not import is a typed refusal, never a raw ImportError.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import decoy_engine
from decoy_engine.detection import _model_status as ms
from decoy_engine.detection import ner
from decoy_engine.plan._checks import check_text_redact_ner_available
from tests.unit.detection_verification._fake_ner_model import make_fake_model

NOT_INSTALLED = "provider.ner_model_not_installed"
NOT_MODELS = ["os", "json", "this", "packaging"]  # importable or installed, but not spaCy models


@pytest.fixture(autouse=True)
def _spacy_present(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only find_spec is faked: the model check must refuse before spaCy is ever imported.
    monkeypatch.setattr(ner, "spacy_installed", lambda: True)
    monkeypatch.delitem(sys.modules, "this", raising=False)


@pytest.mark.parametrize("name", NOT_MODELS)
def test_a_module_that_is_not_a_model_is_refused_and_never_imported(
    name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    status = decoy_engine.ner_status(name)
    assert (status.available, status.reason) == (False, NOT_INSTALLED)
    with pytest.raises(ner.NerUnavailableError) as caught:
        ner.ensure_ner_available(name)
    assert caught.value.code == "ner_model_not_installed"
    with pytest.raises(decoy_engine.ModelUnavailableError) as refused:
        decoy_engine.load_detectors(("regex", "ner"), ner_model=name)
    assert refused.value.reason == NOT_INSTALLED
    assert capsys.readouterr().out == ""
    assert "this" not in sys.modules


@pytest.mark.parametrize("name", NOT_MODELS)
def test_provider_config_ner_model_gets_the_same_refusal(name: str) -> None:
    config: dict[str, Any] = {
        "tables": [
            {
                "name": "p",
                "columns": [
                    {
                        "name": "n",
                        "strategy": "text_redact",
                        "provider_config": {"ner": {"model": name}},
                    }
                ],
            }
        ]
    }
    from decoy_engine.plan import PlanCompileError

    with pytest.raises(PlanCompileError) as caught:
        check_text_redact_ner_available(config)
    assert caught.value.code == "ner_model_not_installed"
    assert "this" not in sys.modules


def test_a_distribution_without_meta_json_is_not_a_model_and_its_code_never_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    name = make_fake_model(
        tmp_path,
        "tripwire_pkg",
        with_meta=False,
        init_code=f"open({str(marker)!r}, 'w').close()\n",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert decoy_engine.ner_status(name).reason == NOT_INSTALLED
    with pytest.raises(ner.NerUnavailableError):
        ner.ensure_ner_available(name)
    with pytest.raises(decoy_engine.ModelUnavailableError):
        decoy_engine.load_detectors(("regex", "ner"), ner_model=name)
    assert not marker.exists()
    assert name not in sys.modules


def test_a_model_distribution_is_found_without_importing_its_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    name = make_fake_model(
        tmp_path, "tripwire_model", init_code=f"open({str(marker)!r}, 'w').close()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert ner.model_installed(name)
    assert ner.installed_model_version(name) == "1.2.3"
    assert not marker.exists()
    assert name not in sys.modules


def test_an_unimportable_spacy_is_a_typed_refusal_not_a_raw_import_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    name = make_fake_model(tmp_path, "fake_ner_ok")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(sys.modules, "spacy", None)  # `import spacy` raises ModuleNotFoundError
    monkeypatch.delitem(sys.modules, "spacy.util", raising=False)

    status = decoy_engine.ner_status(name)
    assert (status.available, status.reason) == (False, "provider.ner_spacy_not_installed")

    with pytest.raises(ner.NerUnavailableError) as caught:
        ner.ensure_ner_available(name)
    assert caught.value.code == "ner_spacy_not_installed"
    assert caught.value.__cause__ is None and caught.value.__context__ is None

    with pytest.raises(decoy_engine.ModelUnavailableError) as refused:
        decoy_engine.load_detectors(("regex", "ner"), ner_model=name)
    assert refused.value.reason == "provider.ner_spacy_not_installed"
    assert refused.value.__cause__ is None and refused.value.__context__ is None

    with pytest.raises(ner.NerUnavailableError):
        ner._pipeline(name)  # the load step is typed too
    _ = ms
