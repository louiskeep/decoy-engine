"""Unit tests for the ML model-pack loader (ML2.1 / §B.5).

Tests:
  - Successful load from a valid pack directory.
  - Missing directory / missing manifest / missing weights.
  - Corrupt manifest JSON.
  - Wrong format_version -> rejected.
  - Wrong feature_schema_version -> rejected.
  - SHA-256 mismatch -> rejected.
  - DECOY_ML_DISABLED=1 env var disables ML.
  - load_with_fallback() returns None (not raises) on any error.
  - ModelPackManifest round-trips through to_dict() / from_dict().

Gate reference: ml-benchmarking-and-privacy.md §B.5 (on-prem only, SHA
verified before joblib.load).
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from decoy_engine.storm.model_pack.loader import ModelPackLoader, ModelPackLoadError
from decoy_engine.storm.model_pack.types import (
    FEATURE_SCHEMA_VERSION,
    PACK_FORMAT,
    ModelPackManifest,
)

# Optional ML extra (joblib / scikit-learn); skip this module when absent so a
# base `.[dev]` CI install (no ml extra) does not fail collection.
joblib = pytest.importorskip("joblib")
DictVectorizer = pytest.importorskip("sklearn.feature_extraction").DictVectorizer

_LIVE_PACK = Path(__file__).parents[2] / "docs" / "v2" / "ml" / "packs" / "lgbm-v1"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _write_minimal_pack(tmp_path: Path, *, weights: bytes | None = None) -> Path:
    """Write a minimal valid pack directory to tmp_path and return it."""
    pack_dir = tmp_path / "test-pack"
    pack_dir.mkdir()

    # Build a trivial model to serialise (joblib has no dumps(); use BytesIO)
    vec = DictVectorizer(sparse=False)
    vec.fit([{"x": 1.0}])
    buf = io.BytesIO()
    joblib.dump({"vec": vec, "clf": None, "classes": ["a", "b"]}, buf)
    blob = buf.getvalue()

    if weights is not None:
        blob = weights

    model_path = pack_dir / "model.joblib"
    model_path.write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()

    manifest = ModelPackManifest(
        pack_id="test",
        version="0.0.1",
        format_version=PACK_FORMAT,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        sha256=sha,
    )
    (pack_dir / "manifest.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    return pack_dir


# ── Live pack (present on disk) ───────────────────────────────────────────────


@pytest.mark.skipif(
    not _LIVE_PACK.exists(), reason="lgbm-v1 pack not found; run train_and_evaluate first"
)
def test_live_pack_loads_successfully() -> None:
    """The committed lgbm-v1 pack loads without error."""
    loader = ModelPackLoader(_LIVE_PACK)
    pack = loader.load()
    assert "vec" in pack
    assert "clf" in pack
    assert "classes" in pack
    assert "manifest" in pack
    manifest = pack["manifest"]
    assert manifest.format_version == PACK_FORMAT
    assert manifest.feature_schema_version == FEATURE_SCHEMA_VERSION


@pytest.mark.skipif(not _LIVE_PACK.exists(), reason="lgbm-v1 pack not found")
def test_live_pack_has_expected_classes() -> None:
    """The committed pack was trained on the expected 11 label classes."""
    loader = ModelPackLoader(_LIVE_PACK)
    pack = loader.load()
    classes = set(pack["classes"])
    expected = {
        "ssn",
        "pan",
        "iban",
        "email",
        "icd10",
        "iso_date",
        "npi",
        "mrn",
        "health_plan_id",
        "cvv",
        "none",
    }
    assert expected.issubset(classes), f"Missing classes: {expected - classes}"


# ── Missing files ─────────────────────────────────────────────────────────────


def test_missing_pack_directory_raises(tmp_path: Path) -> None:
    loader = ModelPackLoader(tmp_path / "nonexistent")
    with pytest.raises(ModelPackLoadError, match="manifest.json"):
        loader.load()


def test_missing_manifest_raises(tmp_path: Path) -> None:
    pack_dir = _write_minimal_pack(tmp_path)
    (pack_dir / "manifest.json").unlink()
    with pytest.raises(ModelPackLoadError, match="manifest.json"):
        ModelPackLoader(pack_dir).load()


def test_missing_weights_raises(tmp_path: Path) -> None:
    pack_dir = _write_minimal_pack(tmp_path)
    (pack_dir / "model.joblib").unlink()
    with pytest.raises(ModelPackLoadError, match="model.joblib"):
        ModelPackLoader(pack_dir).load()


# ── Corrupt / wrong-format manifest ──────────────────────────────────────────


def test_corrupt_manifest_raises(tmp_path: Path) -> None:
    pack_dir = _write_minimal_pack(tmp_path)
    (pack_dir / "manifest.json").write_text("NOT JSON }{", encoding="utf-8")
    with pytest.raises(ModelPackLoadError, match="Corrupt manifest"):
        ModelPackLoader(pack_dir).load()


def test_wrong_format_version_raises(tmp_path: Path) -> None:
    pack_dir = _write_minimal_pack(tmp_path)
    raw = json.loads((pack_dir / "manifest.json").read_text())
    raw["format_version"] = "decoy-model-pack/v99"
    (pack_dir / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ModelPackLoadError, match="format"):
        ModelPackLoader(pack_dir).load()


def test_wrong_feature_schema_version_raises(tmp_path: Path) -> None:
    pack_dir = _write_minimal_pack(tmp_path)
    raw = json.loads((pack_dir / "manifest.json").read_text())
    raw["feature_schema_version"] = "ml99-v99"
    (pack_dir / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ModelPackLoadError, match="Feature schema mismatch"):
        ModelPackLoader(pack_dir).load()


def test_sha256_mismatch_raises(tmp_path: Path) -> None:
    pack_dir = _write_minimal_pack(tmp_path)
    # Tamper with the weights file
    (pack_dir / "model.joblib").write_bytes(b"tampered bytes")
    with pytest.raises(ModelPackLoadError, match="SHA-256 mismatch"):
        ModelPackLoader(pack_dir).load()


def test_empty_sha256_in_manifest_raises(tmp_path: Path) -> None:
    pack_dir = _write_minimal_pack(tmp_path)
    raw = json.loads((pack_dir / "manifest.json").read_text())
    raw["sha256"] = ""
    (pack_dir / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ModelPackLoadError, match="sha256 field is empty"):
        ModelPackLoader(pack_dir).load()


# ── DECOY_ML_DISABLED env var ─────────────────────────────────────────────────


def test_ml_disabled_env_var_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DECOY_ML_DISABLED=1 causes load() to raise."""
    pack_dir = _write_minimal_pack(tmp_path)
    monkeypatch.setenv("DECOY_ML_DISABLED", "1")
    loader = ModelPackLoader(pack_dir)
    assert loader.is_ml_disabled is True
    with pytest.raises(ModelPackLoadError, match="DECOY_ML_DISABLED"):
        loader.load()


# ── load_with_fallback() ──────────────────────────────────────────────────────


def test_fallback_returns_none_on_missing_pack(tmp_path: Path) -> None:
    loader = ModelPackLoader(tmp_path / "nonexistent")
    result = loader.load_with_fallback()
    assert result is None


def test_fallback_returns_none_on_sha_mismatch(tmp_path: Path) -> None:
    pack_dir = _write_minimal_pack(tmp_path)
    (pack_dir / "model.joblib").write_bytes(b"tampered")
    loader = ModelPackLoader(pack_dir)
    result = loader.load_with_fallback()
    assert result is None


def test_fallback_returns_none_when_ml_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack_dir = _write_minimal_pack(tmp_path)
    monkeypatch.setenv("DECOY_ML_DISABLED", "1")
    result = ModelPackLoader(pack_dir).load_with_fallback()
    assert result is None


# ── ModelPackManifest round-trip ──────────────────────────────────────────────


def test_manifest_round_trips() -> None:
    m = ModelPackManifest(
        pack_id="lgbm-v1",
        version="0.1.0",
        sha256="abc123",
        signing_key_ref="unsigned",
        training_seed=42,
        calibration_method="sigmoid",
        operating_threshold=0.1667,
        eval_report_hash="def456",
    )
    d = m.to_dict()
    m2 = ModelPackManifest.from_dict(d)
    assert m2.pack_id == m.pack_id
    assert m2.sha256 == m.sha256
    assert m2.operating_threshold == m.operating_threshold
    assert m2.training_seed == m.training_seed


def test_manifest_ignores_unknown_keys() -> None:
    """from_dict() silently drops unknown keys (forward compatibility)."""
    d = {
        "pack_id": "x",
        "version": "1.0.0",
        "unknown_future_field": "value",
    }
    m = ModelPackManifest.from_dict(d)
    assert m.pack_id == "x"
    assert not hasattr(m, "unknown_future_field")
