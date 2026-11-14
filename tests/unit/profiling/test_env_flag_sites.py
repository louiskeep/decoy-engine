"""The model-pack flags (`DECOY_ML_DISABLED`, `DECOY_PACK_REQUIRE_SIGNATURE`) use the one flag rule.

An unknown spelling on the switch that turns ML off leaves ML off. An unknown spelling on the
signature requirement is refused with the coded, value-free error, and the lenient loader still
degrades to the regex baseline without logging the value.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.errors import EnvFlagError
from decoy_engine.profiling.model_pack._detectors import classifier_status
from decoy_engine.profiling.model_pack.loader import ModelPackLoader, ModelPackLoadError
from decoy_engine.profiling.model_pack.provenance import SIGNING_KEY_ENV
from decoy_engine.profiling.model_pack.types import FEATURE_SCHEMA_VERSION, PACK_FORMAT

WEIGHTS = b"not a real model"
SECRET = "maybe-secret-xyz"


def _pack(tmp_path: Path, **manifest: Any) -> Path:
    directory = tmp_path / "pack"
    directory.mkdir()
    body = {
        "pack_id": "t",
        "version": "9.9.9",
        "format_version": PACK_FORMAT,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "sha256": hashlib.sha256(WEIGHTS).hexdigest(),
        **manifest,
    }
    (directory / "manifest.json").write_text(json.dumps(body))
    (directory / "model.joblib").write_bytes(WEIGHTS)
    return directory


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DECOY_ML_DISABLED", raising=False)
    monkeypatch.delenv("DECOY_PACK_REQUIRE_SIGNATURE", raising=False)
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)


# ----- AT-E3-1: DECOY_ML_DISABLED ------------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
def test_ml_disabled_accepted_true_spellings_disable_ml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    monkeypatch.setenv("DECOY_ML_DISABLED", raw)
    loader = ModelPackLoader(_pack(tmp_path), trusted=True)
    assert loader.is_ml_disabled is True
    with pytest.raises(ModelPackLoadError) as caught:
        loader.load()
    assert caught.value.cause == "disabled"


@pytest.mark.parametrize("raw", ["0", "false", "", "no", "off", None])
def test_ml_disabled_false_empty_or_unset_leaves_ml_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str | None
) -> None:
    if raw is not None:
        monkeypatch.setenv("DECOY_ML_DISABLED", raw)
    assert ModelPackLoader(_pack(tmp_path), trusted=True).is_ml_disabled is False


# ----- AT-E3-2: unknown spellings ------------------------------------------------------------


def test_ml_disabled_unknown_spelling_is_refused_by_the_property(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DECOY_ML_DISABLED", "maybe")
    loader = ModelPackLoader(_pack(tmp_path), trusted=True)
    with pytest.raises(EnvFlagError) as caught:
        loader.is_ml_disabled  # noqa: B018 -- the property read is the call under test
    assert caught.value.code == "env_flag_invalid"
    assert caught.value.variable == "DECOY_ML_DISABLED"


def test_classifier_status_with_an_unknown_ml_disabled_reports_down_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DECOY_ML_DISABLED", "maybe")
    status = classifier_status()
    assert status.available is False
    assert status.cause == "disabled"


def test_require_signature_unknown_spelling_raises_from_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DECOY_PACK_REQUIRE_SIGNATURE", "maybe")
    loader = ModelPackLoader(_pack(tmp_path), trusted=True)
    with pytest.raises(EnvFlagError) as caught:
        loader.load()
    assert caught.value.variable == "DECOY_PACK_REQUIRE_SIGNATURE"


def test_require_signature_unknown_spelling_falls_back_to_no_pack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DECOY_PACK_REQUIRE_SIGNATURE", "maybe")
    assert ModelPackLoader(_pack(tmp_path), trusted=True).load_with_fallback() is None


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_require_signature_true_spellings_still_require_a_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    monkeypatch.setenv("DECOY_PACK_REQUIRE_SIGNATURE", raw)
    loader = ModelPackLoader(_pack(tmp_path), trusted=True)
    with pytest.raises(ModelPackLoadError, match="REQUIRE_SIGNATURE"):
        loader.load()


def test_classifier_reports_down_pack_for_an_unknown_signature_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("lightgbm")
    pytest.importorskip("sklearn")
    monkeypatch.setenv("DECOY_PACK_REQUIRE_SIGNATURE", "maybe")
    status = classifier_status()
    assert status.available is False
    assert status.cause == "pack"


# ----- AT-E3-3: value-free -------------------------------------------------------------------


def test_the_flag_error_and_the_fallback_warning_never_hold_the_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DECOY_PACK_REQUIRE_SIGNATURE", SECRET)
    loader = ModelPackLoader(_pack(tmp_path), trusted=True)
    with pytest.raises(EnvFlagError) as caught:
        loader.load()
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    with caplog.at_level(logging.DEBUG):
        assert loader.load_with_fallback() is None
    assert caplog.records, "the fallback must log a warning"
    assert SECRET not in caplog.text
