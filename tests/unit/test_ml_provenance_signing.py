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

import pytest

from decoy_engine.storm.model_pack.loader import ModelPackLoader, ModelPackLoadError
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

# Optional ML extra (joblib / scikit-learn); skip this module when absent so a
# base `.[dev]` CI install (no ml extra) does not fail collection.
joblib = pytest.importorskip("joblib")
DictVectorizer = pytest.importorskip("sklearn.feature_extraction").DictVectorizer

# ── Fixture key (32 bytes, dev/test only) ─────────────────────────────────────
#
# This is a FIXTURE key for the test suite only.  It must never be used in
# production.  The production key source is an escalated decision (see
# keyMgmtNote in the Sprint C hand-off).

pytestmark = pytest.mark.ml  # ml-gate membership (pytest -m ml)

_FIXTURE_KEY = bytes.fromhex("de00c0de000000000000000000000000000000000000000000000000000000f1")

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


def test_loader_accepts_unsigned_trusted_pack_when_no_key_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TRUSTED unsigned pack loads (with a warning) when no key is configured.

    DE-04 Option C: the trusted first-party default keeps the dev/out-of-box
    warn-and-continue posture. The untrusted counterpart is fail-closed -- see
    test_loader_rejects_unsigned_untrusted_pack_when_no_key.
    """
    pack_dir = _write_signed_pack(tmp_path, key=None)
    # Ensure env var is unset
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)

    loader = ModelPackLoader(pack_dir, trusted=True)
    pack = loader.load()  # should not raise
    assert "manifest" in pack


def test_loader_rejects_unsigned_untrusted_pack_when_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DE-04 Option C core guarantee: an UNTRUSTED unsigned pack is refused
    before joblib.load even with no key and the flag unset."""
    pack_dir = _write_signed_pack(tmp_path, key=None)
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    monkeypatch.delenv("DECOY_PACK_REQUIRE_SIGNATURE", raising=False)

    loader = ModelPackLoader(pack_dir)  # default trusted=False
    with pytest.raises(ModelPackLoadError, match="untrusted boundary"):
        loader.load()


def test_loader_accepts_signed_untrusted_pack_with_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An UNTRUSTED pack loads when it carries a verifiable signature -- the
    escape hatch: verified provenance lets an external pack cross the boundary."""
    pack_dir = _write_signed_pack(tmp_path, key=_FIXTURE_KEY)
    monkeypatch.setenv(SIGNING_KEY_ENV, _FIXTURE_KEY_HEX)
    monkeypatch.delenv("DECOY_PACK_REQUIRE_SIGNATURE", raising=False)

    loader = ModelPackLoader(pack_dir)  # untrusted, but signed + key present
    pack = loader.load()
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


# ── Option A: derive_pack_signing_key (instance-master-key-derived) ───────────

_FIXTURE_MASTER = bytes.fromhex("00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff")


def test_derive_pack_signing_key_is_32_bytes_and_deterministic() -> None:
    """Deriving from the same 32-byte master yields the same 32-byte key."""
    from decoy_engine.storm.model_pack.provenance import derive_pack_signing_key

    k1 = derive_pack_signing_key(_FIXTURE_MASTER)
    k2 = derive_pack_signing_key(_FIXTURE_MASTER)
    assert isinstance(k1, bytes)
    assert len(k1) == 32
    assert k1 == k2, "derivation must be deterministic for a given master"


def test_derive_pack_signing_key_differs_from_master_and_other_labels() -> None:
    """The derived key is not the master itself and is purpose-separated."""
    from decoy_engine.determinism._hkdf import hkdf_sha256
    from decoy_engine.storm.model_pack.provenance import (
        PACK_SIGNING_INFO,
        derive_pack_signing_key,
    )

    derived = derive_pack_signing_key(_FIXTURE_MASTER)
    assert derived != _FIXTURE_MASTER
    # A different info label must produce a different key (domain separation).
    other = hkdf_sha256(
        ikm=_FIXTURE_MASTER,
        salt=b"\x00" * 32,
        info=b"pipeline:something-else",
        length=32,
    )
    assert derived != other
    assert PACK_SIGNING_INFO == "decoy-pack-signing-v1"


def test_derive_pack_signing_key_rejects_wrong_length() -> None:
    """A non-32-byte master is rejected (matches make_key_resolver's contract)."""
    from decoy_engine.storm.model_pack.provenance import derive_pack_signing_key

    with pytest.raises(ValueError):
        derive_pack_signing_key(b"too-short")


def test_derived_key_round_trips_through_sign_verify() -> None:
    """A manifest signed with the derived key verifies with the derived key."""
    from decoy_engine.storm.model_pack.provenance import derive_pack_signing_key

    key = derive_pack_signing_key(_FIXTURE_MASTER)
    m = _make_manifest()
    m.manifest_hmac = sign_manifest(m, key)
    assert verify_manifest(m, key) is True
    # A key derived from a different master must NOT verify.
    other_key = derive_pack_signing_key(bytes(32))
    assert verify_manifest(m, other_key) is False


# ── Option A: sign_pack (install/deploy-time in-place signing) ────────────────


def test_sign_pack_signs_in_place_and_loader_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sign_pack writes a valid HMAC that the loader then accepts with the key set."""
    from decoy_engine.storm.model_pack.provenance import derive_pack_signing_key, sign_pack

    pack_dir = _write_signed_pack(tmp_path, key=None)  # unsigned on disk
    key = derive_pack_signing_key(_FIXTURE_MASTER)

    digest = sign_pack(pack_dir, key)
    assert len(digest) == 64

    # Manifest on disk now carries the signature.
    raw = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    assert raw["manifest_hmac"] == digest

    # Loader accepts it when the matching key is configured.
    monkeypatch.setenv(SIGNING_KEY_ENV, key.hex())
    loader = ModelPackLoader(pack_dir)
    pack = loader.load()  # must not raise
    assert "manifest" in pack


def test_sign_pack_rejects_empty_key(tmp_path: Path) -> None:
    """sign_pack refuses an empty key rather than producing a weak signature."""
    from decoy_engine.storm.model_pack.provenance import sign_pack

    pack_dir = _write_signed_pack(tmp_path, key=None)
    with pytest.raises(ValueError):
        sign_pack(pack_dir, b"")


# ── Option A: DECOY_PACK_REQUIRE_SIGNATURE fail-closed posture ────────────────


def test_require_signature_without_key_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQUIRE_SIGNATURE=1 with no key configured -> hard error, not a warning."""
    pack_dir = _write_signed_pack(tmp_path, key=None)
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    monkeypatch.setenv("DECOY_PACK_REQUIRE_SIGNATURE", "1")

    # trusted=True so the flag (not the untrusted-boundary rule) is the sole
    # driver of the requirement -- this test targets the opt-in hard-lockdown.
    loader = ModelPackLoader(pack_dir, trusted=True)
    with pytest.raises(ModelPackLoadError, match="REQUIRE_SIGNATURE"):
        loader.load()


def test_require_signature_with_signed_pack_and_key_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQUIRE_SIGNATURE=1 + key + properly signed pack -> loads ok (fail-closed allows the good path)."""
    from decoy_engine.storm.model_pack.provenance import derive_pack_signing_key, sign_pack

    pack_dir = _write_signed_pack(tmp_path, key=None)
    key = derive_pack_signing_key(_FIXTURE_MASTER)
    sign_pack(pack_dir, key)
    monkeypatch.setenv(SIGNING_KEY_ENV, key.hex())
    monkeypatch.setenv("DECOY_PACK_REQUIRE_SIGNATURE", "1")

    loader = ModelPackLoader(pack_dir)
    pack = loader.load()
    assert "manifest" in pack


def test_require_signature_accepts_truthy_word_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: DECOY_PACK_REQUIRE_SIGNATURE=true (not just '1') enables fail-closed."""
    pack_dir = _write_signed_pack(tmp_path, key=None)
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    monkeypatch.setenv("DECOY_PACK_REQUIRE_SIGNATURE", "TRUE")

    loader = ModelPackLoader(pack_dir, trusted=True)  # flag is the sole driver
    with pytest.raises(ModelPackLoadError, match="REQUIRE_SIGNATURE"):
        loader.load()


def test_require_signature_ambiguous_value_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: an unrecognized flag value raises rather than silently failing open."""
    pack_dir = _write_signed_pack(tmp_path, key=None)
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    monkeypatch.setenv("DECOY_PACK_REQUIRE_SIGNATURE", "maybe")

    loader = ModelPackLoader(pack_dir)
    with pytest.raises(ModelPackLoadError, match="unrecognized value"):
        loader.load()


def test_require_signature_falsy_word_disables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit falsy token ('off') keeps the warn-and-continue dev behaviour."""
    pack_dir = _write_signed_pack(tmp_path, key=None)
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    monkeypatch.setenv("DECOY_PACK_REQUIRE_SIGNATURE", "off")

    # trusted=True: with the flag explicitly off and a trusted pack, the
    # warn-and-continue dev posture holds (an untrusted pack would still fail).
    loader = ModelPackLoader(pack_dir, trusted=True)
    pack = loader.load()  # must not raise
    assert "manifest" in pack
