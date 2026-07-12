"""Tests for the opt-in fail-closed strict provenance mode (DE-04).

Defect background (adversarial-review finding DE-04):
    ``ModelPackLoader`` verifies a SHA-256 that the adjacent ``manifest.json``
    declares about ITSELF (not an independent authentication), accepts an
    empty signature by default, and the per-instance HMAC check is only
    enforced when a signing key happens to be configured. Combined with a
    caller-suppliable ``pack_dir`` (see ``classify_fields(pack_dir=...)``),
    an attacker who can influence the pack path can point the loader at a
    self-consistent (self-signed-hash) but unauthenticated artifact and have
    it deserialised via ``joblib.load`` (arbitrary code execution on
    unpickling).

Fix under test:
    An OPT-IN ``require_authenticated_provenance`` constructor flag on
    ``ModelPackLoader``, default ``False`` (byte-for-byte unchanged default
    behaviour -- see test_ml_model_pack.py / test_ml_provenance_signing.py,
    which must keep passing unmodified). When ``True``, ``load()`` fails
    CLOSED, before any ``joblib.load`` deserialisation:
      - an absent/empty ``manifest_hmac`` is rejected even if no signing key
        is configured (closes the "no key -> silently accepted" gap);
      - an HMAC mismatch against a configured verification key is rejected
        (pre-existing behaviour, re-asserted here under the new flag);
      - a tampered weights blob (SHA-256 mismatch) is rejected (pre-existing
        behaviour, re-asserted here under the new flag);
      - a path-substituted pack directory (path traversal or a symlink that
        escapes the declared ``allowed_root``) is rejected.

    The default-flip to strict-by-default is explicitly OUT OF SCOPE here --
    it is coupled to the packaging/signing rollout (DE-07) and must remain a
    separate, gated migration. Replacing joblib/pickle with ONNX/skops is
    also out of scope (larger fork); see the loader module docstring.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from decoy_engine.storm.model_pack.loader import ModelPackLoader, ModelPackLoadError
from decoy_engine.storm.model_pack.provenance import SIGNING_KEY_ENV, sign_manifest
from decoy_engine.storm.model_pack.types import (
    FEATURE_SCHEMA_VERSION,
    PACK_FORMAT,
    ModelPackManifest,
)

# Optional ML extra (joblib / scikit-learn); skip this module when absent so a
# base `.[dev]` CI install (no ml extra) does not fail collection.
joblib = pytest.importorskip("joblib")
DictVectorizer = pytest.importorskip("sklearn.feature_extraction").DictVectorizer

_FIXTURE_KEY = bytes.fromhex("de00c0de000000000000000000000000000000000000000000000000000000f1")


def _write_pack(
    root: Path,
    name: str = "pack",
    *,
    manifest_hmac: str = "",
    weights: bytes | None = None,
) -> Path:
    """Write a minimal valid (self-consistent) pack directory under *root*."""
    pack_dir = root / name
    pack_dir.mkdir(parents=True, exist_ok=True)

    vec = DictVectorizer(sparse=False)
    vec.fit([{"x": 1.0}])
    buf = io.BytesIO()
    joblib.dump({"vec": vec, "clf": None, "classes": ["a", "b"]}, buf)
    blob = buf.getvalue()
    if weights is not None:
        blob = weights

    (pack_dir / "model.joblib").write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()

    manifest = ModelPackManifest(
        pack_id="test",
        version="0.0.1",
        format_version=PACK_FORMAT,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        sha256=sha,
        manifest_hmac=manifest_hmac,
    )
    (pack_dir / "manifest.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    return pack_dir


def _assert_never_deserialised(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Patch ModelPackLoader._deserialise to record whether it was ever reached."""
    called: list[bool] = []

    def _boom(self: ModelPackLoader, model_bytes: bytes) -> dict:
        called.append(True)
        raise AssertionError("joblib deserialisation must not be reached")

    monkeypatch.setattr(ModelPackLoader, "_deserialise", _boom)
    return called


# ── Default (non-strict) mode is unaffected ──────────────────────────────────


def test_default_mode_still_accepts_unsigned_pack_no_key(tmp_path: Path) -> None:
    """require_authenticated_provenance omitted -> prior warn-and-accept behaviour."""
    pack_dir = _write_pack(tmp_path)
    loader = ModelPackLoader(pack_dir)  # no new kwargs at all
    pack = loader.load()  # must not raise
    assert "manifest" in pack


def test_default_mode_explicit_false_matches_omitted(tmp_path: Path) -> None:
    pack_dir = _write_pack(tmp_path)
    loader = ModelPackLoader(pack_dir, require_authenticated_provenance=False)
    pack = loader.load()
    assert "manifest" in pack


# ── Strict mode: unsigned / empty signature ──────────────────────────────────


def test_strict_mode_rejects_empty_signature_with_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict mode rejects an empty manifest_hmac even when no key is configured.

    This is the gap the default (non-strict) mode leaves open: with no
    DECOY_PACK_SIGNING_KEY configured, an unsigned pack is normally accepted
    with a warning. Strict mode must fail closed instead.
    """
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    monkeypatch.delenv("DECOY_PACK_REQUIRE_SIGNATURE", raising=False)
    pack_dir = _write_pack(tmp_path, manifest_hmac="")
    called = _assert_never_deserialised(monkeypatch)

    loader = ModelPackLoader(pack_dir, require_authenticated_provenance=True, allowed_root=tmp_path)
    with pytest.raises(ModelPackLoadError):
        loader.load()
    assert not called, "joblib.load path must not be reached when signature is rejected"


def test_strict_mode_rejects_empty_signature_with_key_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SIGNING_KEY_ENV, _FIXTURE_KEY.hex())
    monkeypatch.delenv("DECOY_PACK_REQUIRE_SIGNATURE", raising=False)
    pack_dir = _write_pack(tmp_path, manifest_hmac="")
    called = _assert_never_deserialised(monkeypatch)

    loader = ModelPackLoader(pack_dir, require_authenticated_provenance=True, allowed_root=tmp_path)
    with pytest.raises(ModelPackLoadError, match="unsigned"):
        loader.load()
    assert not called


# ── Strict mode: wrong-signer / HMAC mismatch ───────────────────────────────


def test_strict_mode_rejects_hmac_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_KEY_ENV, _FIXTURE_KEY.hex())
    monkeypatch.delenv("DECOY_PACK_REQUIRE_SIGNATURE", raising=False)
    # A plausible-looking but wrong HMAC (signed by a different / no key).
    pack_dir = _write_pack(tmp_path, manifest_hmac="a" * 64)
    called = _assert_never_deserialised(monkeypatch)

    loader = ModelPackLoader(pack_dir, require_authenticated_provenance=True, allowed_root=tmp_path)
    with pytest.raises(ModelPackLoadError, match="signature verification FAILED"):
        loader.load()
    assert not called


# ── Strict mode: altered content (SHA-256 mismatch) ─────────────────────────


def test_strict_mode_rejects_altered_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    monkeypatch.delenv("DECOY_PACK_REQUIRE_SIGNATURE", raising=False)
    pack_dir = _write_pack(tmp_path)
    # Tamper with the weights AFTER the (self-declared) sha256 was written.
    (pack_dir / "model.joblib").write_bytes(b"attacker-controlled-payload")
    called = _assert_never_deserialised(monkeypatch)

    loader = ModelPackLoader(pack_dir, require_authenticated_provenance=True, allowed_root=tmp_path)
    with pytest.raises(ModelPackLoadError, match="SHA-256 mismatch"):
        loader.load()
    assert not called


# ── Strict mode: path-substituted pack (traversal / symlink escape) ────────


def test_strict_mode_rejects_path_traversal_outside_allowed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed_root = tmp_path / "trusted-packs"
    allowed_root.mkdir()
    outside_root = tmp_path / "evil-outside"
    _write_pack(outside_root, name="lgbm-v1")
    # Reference the outside pack via a traversal path rooted under allowed_root.
    traversal_path = allowed_root / ".." / "evil-outside" / "lgbm-v1"
    called = _assert_never_deserialised(monkeypatch)

    loader = ModelPackLoader(
        traversal_path,
        require_authenticated_provenance=True,
        allowed_root=allowed_root,
    )
    with pytest.raises(ModelPackLoadError, match="outside the allowed root"):
        loader.load()
    assert not called


def test_strict_mode_rejects_symlink_escape_outside_allowed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed_root = tmp_path / "trusted-packs"
    allowed_root.mkdir()
    outside_root = tmp_path / "evil-outside"
    real_pack_dir = _write_pack(outside_root, name="lgbm-v1")

    # A symlink INSIDE the allowed root that escapes to the outside pack.
    symlinked_pack = allowed_root / "lgbm-v1"
    symlinked_pack.symlink_to(real_pack_dir, target_is_directory=True)
    called = _assert_never_deserialised(monkeypatch)

    loader = ModelPackLoader(
        symlinked_pack,
        require_authenticated_provenance=True,
        allowed_root=allowed_root,
    )
    with pytest.raises(ModelPackLoadError, match="outside the allowed root"):
        loader.load()
    assert not called


def test_strict_mode_accepts_pack_within_allowed_root(tmp_path: Path) -> None:
    """Strict mode is not merely a blanket rejection: a properly-rooted,
    properly-signed pack still loads."""
    allowed_root = tmp_path / "trusted-packs"
    allowed_root.mkdir()
    pack_dir = allowed_root / "lgbm-v1"
    pack_dir.mkdir()

    vec = DictVectorizer(sparse=False)
    vec.fit([{"x": 1.0}])
    buf = io.BytesIO()
    joblib.dump({"vec": vec, "clf": None, "classes": ["a", "b"]}, buf)
    blob = buf.getvalue()
    (pack_dir / "model.joblib").write_bytes(blob)
    sha = hashlib.sha256(blob).hexdigest()

    manifest = ModelPackManifest(
        pack_id="test",
        version="0.0.1",
        format_version=PACK_FORMAT,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        sha256=sha,
    )
    manifest.manifest_hmac = sign_manifest(manifest, _FIXTURE_KEY)
    (pack_dir / "manifest.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

    import os

    os.environ[SIGNING_KEY_ENV] = _FIXTURE_KEY.hex()
    try:
        loader = ModelPackLoader(
            pack_dir, require_authenticated_provenance=True, allowed_root=allowed_root
        )
        pack = loader.load()
        assert "manifest" in pack
    finally:
        del os.environ[SIGNING_KEY_ENV]


def test_strict_mode_without_allowed_root_is_fail_closed_config_error(
    tmp_path: Path,
) -> None:
    """Strict mode cannot verify path containment without a declared trust
    boundary; omitting allowed_root is itself a hard error (never silently
    skipped), matching the module's existing fail-closed posture for
    ambiguous security configuration (see _require_signature_enabled)."""
    pack_dir = _write_pack(tmp_path)
    loader = ModelPackLoader(pack_dir, require_authenticated_provenance=True)
    with pytest.raises(ModelPackLoadError, match="allowed_root"):
        loader.load()
