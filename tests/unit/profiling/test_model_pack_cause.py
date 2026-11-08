"""`ModelPackLoadError.cause` is a field set at each raise site, and `classify_frame`
runs on a pack it is given, never one it loads itself."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.profiling.model_pack import classify as classify_mod
from decoy_engine.profiling.model_pack.loader import ModelPackLoader, ModelPackLoadError
from decoy_engine.profiling.model_pack.types import FEATURE_SCHEMA_VERSION, PACK_FORMAT

WEIGHTS = b"not a real model"


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


def _cause(loader: ModelPackLoader) -> str:
    with pytest.raises(ModelPackLoadError) as caught:
        loader.load()
    return caught.value.cause


def test_disabled_is_a_cause_read_from_the_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DECOY_ML_DISABLED", "1")
    assert _cause(ModelPackLoader(_pack(tmp_path), trusted=True)) == "disabled"


@pytest.mark.parametrize(
    "case",
    ["missing_dir", "bad_sha", "bad_format", "bad_schema", "no_weights", "corrupt_manifest"],
)
def test_every_pack_defect_is_cause_pack(tmp_path: Path, case: str) -> None:
    monkeypatch_free = {
        "bad_sha": {"sha256": "0" * 64},
        "bad_format": {"format_version": "other/v9"},
        "bad_schema": {"feature_schema_version": "ml9-v9"},
    }
    if case == "missing_dir":
        loader = ModelPackLoader(tmp_path / "nowhere", trusted=True)
    else:
        directory = _pack(tmp_path, **monkeypatch_free.get(case, {}))
        if case == "no_weights":
            (directory / "model.joblib").unlink()
        if case == "corrupt_manifest":
            (directory / "manifest.json").write_text("{not json")
        loader = ModelPackLoader(directory, trusted=True)
    assert _cause(loader) == "pack"


def test_a_missing_joblib_is_cause_import(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "joblib", None)
    assert _cause(ModelPackLoader(_pack(tmp_path), trusted=True)) == "import"


def test_the_cause_is_never_parsed_from_the_message(tmp_path: Path) -> None:
    error = ModelPackLoadError(
        "ML is disabled via DECOY_ML_DISABLED=1; import joblib", cause="pack"
    )
    assert error.cause == "pack"
    assert ModelPackLoadError("anything").cause == "pack"


def _fake_pack(label: str | None = "ssn", prob: float = 0.9) -> dict[str, Any]:
    class Vec:
        def transform(self, rows: list[dict[str, float]]) -> list[list[float]]:
            return [[0.0] for _ in rows]

    class Clf:
        classes_ = np.array(["none", "ssn"])

        def predict_proba(self, matrix: Any) -> np.ndarray:
            return np.array([[1.0 - prob, prob]])

    manifest = SimpleNamespace(
        operating_threshold=0.167,
        pack_id="lgbm-v1",
        version="0.1.0",
        feature_schema_version="ml1-v1",
    )
    return {"vec": Vec(), "clf": Clf(), "manifest": manifest}


def test_classify_frame_uses_only_the_pack_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("classify_frame must not load a pack")

    monkeypatch.setattr(ModelPackLoader, "load", boom)
    monkeypatch.setattr(ModelPackLoader, "load_with_fallback", boom)
    monkeypatch.setattr(classify_mod, "_default_pack_dir", boom)
    result = classify_mod.classify_frame(_fake_pack(), pd.DataFrame({"c": ["123-45-6789"] * 4}))
    assert set(result) == {"c"}
    assert result["c"]["label"] == "ssn"
    assert result["c"]["model_pack_id"] == "lgbm-v1"
    assert "123-45-6789" not in json.dumps(result)


def test_classify_fields_is_a_wrapper_over_classify_frame_and_stays_lenient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame({"c": ["x"] * 3})
    monkeypatch.setattr(ModelPackLoader, "load_with_fallback", lambda self: None)
    assert classify_mod.classify_fields(frame) is None  # lenient path unchanged
    seen: list[Any] = []
    pack = _fake_pack()
    monkeypatch.setattr(ModelPackLoader, "load_with_fallback", lambda self: pack)
    original = classify_mod.classify_frame
    monkeypatch.setattr(
        classify_mod, "classify_frame", lambda p, f: seen.append(p) or original(p, f)
    )
    out = classify_mod.classify_fields(frame)
    assert seen == [pack]
    assert out is not None and out["c"]["label"] == "ssn"


def test_default_pack_loader_is_the_one_trusted_loader_classify_fields_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.profiling.model_pack import default_pack_loader

    loader = default_pack_loader()
    assert loader._trusted is True  # the shipped default pack, and nothing else, is trusted
    asked: list[Any] = []
    monkeypatch.setattr(
        classify_mod, "default_pack_loader", lambda fa=None: asked.append(fa) or loader
    )
    monkeypatch.setattr(ModelPackLoader, "load_with_fallback", lambda self: None)
    assert classify_mod.classify_fields(pd.DataFrame({"c": ["x"]})) is None
    assert asked == [None]
