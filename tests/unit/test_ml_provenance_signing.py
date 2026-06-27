"""Tests for HMAC-SHA256 model-pack provenance signing (ML3.2).

Gate reference: Sprint C ML3.2 spec.

Tests:
  - sign_manifest returns a 64-char hex string.
  - verify_manifest returns True on a valid (fixture) key round-trip.
  - verify_manifest returns False after tampering with any manifest field.
  - verify_manifest returns False with a wrong key.
  - verify_manifest returns False when manifest_hmac is empty.
  - Loader: DECOY_PACK_SIGNING_KEY set + matching signature -> loads ok.
  - Loader: DECOY_PACK_SIGNING_KEY set + unsigned pack -> ModelPackLoadError.
  - Loader: DECOY_PACK_SIGNING_KEY set + mismatched HMAC -> ModelPackLoadError.
  - Loader: no key set + unsigned pack -> loads with warning (accepted).
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import joblib  # type: ignore[import]
import pytest
from sklearn.feature_extraction import DictVectorizer

from decoy_engine.storm.model_pack.loader import ModelPackLoadError, ModelPackLoader
from decoy_engine.storm.model_pack.provenance import (
    SIGNING_KEY_ENV,
    sign_manifest,
    verify_manifest,
)
from decoy_engine.storm.model_pack.types import (
    FEATURE_SCHEMA_VERSION,
    PACK_FORMAT,
    ModelPackManifest,
)

# ── Fixture key (32 bytes, dev/test only) ─────────────────────────────────────
#
# This is a FIXTURE key for the test suite only.  It must never be used in
# production.  The production key source is an escalated decision (see
# keyMgmtNote in the Sprint C hand-off).

_FIXTURE_KEY = bytes.fromhex(
    "de00c0de000000000000000000000000000000000000000000000000000000f1"
)

_FIXTURE_KEY_HEX = _FIXTURE_KEY.hex()


def _make_manifest(**overrides) -> ModelPackManifest:
    """Return a minimal ModelPackManifest for testing."""
    defaults = dict(
        pack_id="lgbm-v1",
        version="0.1.0",
        sha256="abc123deadbeef",
        eval_report_hash="deadbeef123456",
        training_seed=42,
        calibration_method="sigmoid",
        operating_threshold=0.1667,
    )
    defaults.update(overrides)
    return ModelPackManifest(**defaults)


def _write_signed_pack(
    tmp_path: Path,
    key: bytes | None = None,
    hmac_override: str | None = None,
) -> Path:
    """Write a minimal valid pack directory to tmp_path.

    If *key* is provided the manifest is signed.  If *hmac_override* is
    provided it is stored directly (allows testing tampered HMAC).
    """
    pack_dir = tmp_path / "signed-pack"
    pack_dir.mkdir()

    # Minimal sklearn DictVectorizer + placeholder clf
    vec = DictVectorizer(sparse=False)
    vec.fit([{"x": 1.0}])
    buf = io.BytesIO()
    joblib.dump({"vec": vec, "clf": None, "classes": ["ssn", "none"]}, buf)
    blob = buf.getvalue()

    model_path = pack_dir / "model.joblib"
    model_path.write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()

    manifest = ModelPackManifest(
        pack_id="test",
        version="0.0.1",
        format_version=PACK_FORMAT,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        sha256=sha,
        eval_report_hash="abc123",
    )

    if key is not None:
        manifest.manifest_hmac = sign_manifest(manifest, key)

    if hmac_override is not None:
        manifest.manifest_hmac = hmac_override

    (pack_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), sort_keys=True), encoding="utf-8"
    )
    return pack_dir


# ── sign_manifest / verify_manifest unit tests ───────────────────────────────


def test_sign_returns_64_char_hex() -> None:
    """sign_manifest returns a 64-character lower-case hex string (SHA-256 width)."""
    m = _make_manifest()
    sig = sign_manifest(m, _FIXTURE_KEY)
    assert isinstance(sig, str)
    assert len(sig) == 64, f"Expected 64 hex chars, got {len(sig)}"
    assert sig == sig.lower(), "HMAC hex must be lower-case"


def test_round_trip_verify() -> None:
    """sign then verify with the same key returns True."""
    m = _make_manifest()
    m.manifest_hmac = sign_manifest(m, _FIXTURE_KEY)
    assert verify_manifest(m, _FIXTURE_KEY) is True


def test_verify_wrong_key_returns_false() -> None:
    """verify_manifest with a different key returns False."""
    m = _make_manifest()
    m.manifest_hmac = sign_manifest(m, _FIXTURE_KEY)
    wrong_key = b"\xff" * 32
    assert verify_manifest(m, wrong_key) is False


def test_verify_unsigned_returns_false() -> None:
    """verify_manifest when manifest_hmac is empty returns False."""
    m = _make_manifest()
    assert m.manifest_hmac == ""
    assert verify_manifest(m, _FIXTURE_KEY) is False


def test_tamper_sha256_invalidates_signature() -> None:
    """Mutating manifest.sha256 after signing invalidates the HMAC (tamper detection)."""
    m = _make_manifest()
    m.manifest_hmac = sign_manifest(m, _FIXTURE_KEY)
    # Tamper: change the model weights hash
    m.sha256 = "00000000000000000000000000000000000000000000000000000000000000000"
    assert verify_manifest(m, _FIXTURE_KEY) is False


def test_tamper_eval_report_hash_invalidates_signature() -> None:
    """Mutating manifest.eval_report_hash after signing invalidates the HMAC."""
    m = _make_manifest()
    m.manifest_hmac = sign_manifest(m, _FIXTURE_KEY)
    m.eval_report_hash = "tampered"
    assert verify_manifest(m, _FIXTURE_KEY) is False


def test_tamper_pack_id_invalidates_signature() -> None:
    """Mutating manifest.pack_id after signing invalidates the HMAC."""
    m = _make_manifest()
    m.manifest_hmac = sign_manifest(m, _FIXTURE_KEY)
    m.pack_id = "evil-pack-v99"
    assert verify_manifest(m, _FIXTURE_KEY) is False


def test_sign_is_deterministic() -> None:
    """sign_manifest is deterministic: same manifest + same key -> same HMAC."""
    m = _make_manifest()
    sig_a = sign_manifest(m, _FIXTURE_KEY)
    sig_b = sign_manifest(m, _FIXTURE_KEY)
    assert sig_a == sig_b


def test_sign_manifest_empty_key_raises() -> None:
    """sign_manifest raises ValueError on empty key."""
    m = _make_manifest()
    with pytest.raises(ValueError, match="non-empty"):
        sign_manifest(m, b"")


# ── Loader integration: signature enforcement ─────────────────────────────────


def test_loader_accepts_signed_pack_with_matching_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loader accepts a signed pack when DECOY_PACK_SIGNING_KEY matches."""
    pack_dir = _write_signed_pack(tmp_path, key=_FIXTURE_KEY)
    monkeypatch.setenv(SIGNING_KEY_ENV, _FIXTURE_KEY_HEX)

    loader = ModelPackLoader(pack_dir)
    pack = loader.load()
    assert "manifest" in pack


def test_loader_rejects_unsigned_pack_when_key_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loader raises ModelPackLoadError for an unsigned pack when key is set."""
    pack_dir = _write_signed_pack(tmp_path, key=None)  # no signature
    monkeypatch.setenv(SIGNING_KEY_ENV, _FIXTURE_KEY_HEX)

    loader = ModelPackLoader(pack_dir)
    with pytest.raises(ModelPackLoadError, match="unsigned"):
        loader.load()


def test_loader_rejects_tampered_hmac_when_key_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loader raises ModelPackLoadError when HMAC does not match the key."""
    # Write a pack with a plausible-looking but wrong HMAC
    pack_dir = _write_signed_pack(tmp_path, hmac_override="a" * 64)
    monkeypatch.setenv(SIGNING_KEY_ENV, _FIXTURE_KEY_HEX)

    loader = ModelPackLoader(pack_dir)
    with pytest.raises(ModelPackLoadError, match="signature verification FAILED"):
        loader.load()


def test_loader_accepts_unsigned_pack_when_no_key_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loader accepts an unsigned pack (with a warning) when no key is configured."""
    pack_dir = _write_signed_pack(tmp_path, key=None)
    # Ensure env var is unset
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)

    loader = ModelPackLoader(pack_dir)
    pack = loader.load()  # should not raise
    assert "manifest" in pack


def test_load_with_fallback_returns_none_on_signature_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_with_fallback returns None (not raises) when HMAC check fails."""
    pack_dir = _write_signed_pack(tmp_path, hmac_override="b" * 64)
    monkeypatch.setenv(SIGNING_KEY_ENV, _FIXTURE_KEY_HEX)

    loader = ModelPackLoader(pack_dir)
    result = loader.load_with_fallback()
    assert result is None
