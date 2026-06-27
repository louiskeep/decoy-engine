"""HMAC-SHA256 provenance signing for model-pack manifests (ML3.2).

Pattern: HMAC-SHA256 (RFC 2104, stdlib).
  Reference: https://datatracker.ietf.org/doc/html/rfc2104
  Rationale: stdlib-only keyed-hash primitive used throughout the engine
  (internal/crypto.py hmac_hex, determinism/_hkdf.py). We do NOT use
  the ``cryptography`` package for consistency with existing keyed-hash
  surface (see determinism/_hkdf.py lines 6-11).

Signing algorithm (``sign_manifest``):
    1. Serialise the manifest to canonical JSON: ``json.dumps(d, sort_keys=True,
       separators=(',', ':'))`` with ``manifest_hmac`` field cleared to ``""``.
    2. Compute ``HMAC-SHA256(key, canonical_bytes)``.
    3. Return the hex digest (64 chars).  Store it in ``manifest.manifest_hmac``.

Verification (``verify_manifest``):
    Re-derive the expected HMAC from the manifest (``manifest_hmac`` cleared),
    then ``hmac.compare_digest(expected, stored)`` -- constant-time comparison
    to prevent timing side-channels.

Integrity scope:
    Signing binds ALL manifest fields except ``manifest_hmac`` itself,
    including:
      - ``sha256``           -- model.joblib weights hash (ML2.1)
      - ``eval_report_hash`` -- training metric evidence hash (§B.7)
      - ``feature_schema_version``, ``pack_id``, ``version``, etc.
    Tampering with any of these fields invalidates the HMAC.

Key source (DECIDED -- Option A, instance-master-key-derived):
    Decoy is a single-tenant, self-hosted deployment: the engine and pack
    run on the operator's own box, on the operator's own network, and the
    pack never leaves that box.  Within this trust boundary a symmetric
    HMAC is the appropriate signature primitive (an attacker who can swap
    the pack file already controls the host, so the signature is
    tamper-evidence / integrity, not defence against a remote forger).
    There is no cross-organisation distribution, so asymmetric signatures
    (Ed25519/cosign) and an external KMS would add operational cost with no
    threat-model benefit here.

    The signing key is therefore DERIVED from the instance master key via
    HKDF-SHA256 (RFC 5869) with the fixed info label
    ``decoy-pack-signing-v1`` -- see :func:`derive_pack_signing_key`, which
    routes through the same canonical ``determinism._hkdf.hkdf_sha256`` and
    salt convention as ``context._hkdf_sha256``.  No new long-lived secret
    is introduced: the master key already exists on the box.

    Because the key is per-instance, packs are signed at INSTALL/DEPLOY time
    on the target box (see :func:`sign_pack`), not at build time in the
    repository.  The committed pack therefore ships with an empty
    ``manifest_hmac`` (unsigned); the deploy step derives the key and signs
    in place.  At runtime the loader reads the key from
    ``DECOY_PACK_SIGNING_KEY`` (hex), and the platform sets
    ``DECOY_PACK_REQUIRE_SIGNATURE=1`` so an unsigned/altered pack is
    rejected fail-closed in production (development leaves it unset).

References:
    RFC 2104 (HMAC): https://datatracker.ietf.org/doc/html/rfc2104
    RFC 5869 (HKDF): https://datatracker.ietf.org/doc/html/rfc5869
    Python stdlib ``hmac``: https://docs.python.org/3/library/hmac.html
"""

from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import json
import logging
import os
from pathlib import Path

from decoy_engine.storm.model_pack.types import ModelPackManifest

_log = logging.getLogger(__name__)

#: Environment variable name for the hex-encoded pack-signing key (32 bytes = 64 hex chars).
SIGNING_KEY_ENV = "DECOY_PACK_SIGNING_KEY"

#: HKDF info label binding a derived sub-key to the pack-signing purpose (Option A).
#: Versioned so a future rotation can use ``decoy-pack-signing-v2`` without colliding.
PACK_SIGNING_INFO = "decoy-pack-signing-v1"


def derive_pack_signing_key(master: bytes) -> bytes:
    """Derive the 32-byte pack-signing key from the instance *master* key.

    Option A (instance-master-key-derived signing). HKDF-SHA256 (RFC 5869)
    with the fixed info label :data:`PACK_SIGNING_INFO`, routed through the
    canonical ``determinism._hkdf.hkdf_sha256`` and the same zero-salt
    convention as ``context._hkdf_sha256`` so the byte stream matches the
    rest of the engine's keyed derivations.

    The master key already exists on the box (the same key the mask/generate
    resolvers are built from); no new long-lived secret is introduced.

    Parameters
    ----------
    master:
        The instance master key. Must be exactly 32 bytes (matching
        ``make_key_resolver``'s contract).

    Returns
    -------
    bytes
        A 32-byte signing key, suitable for :func:`sign_manifest` /
        :func:`verify_manifest`.

    Raises
    ------
    ValueError
        If ``master`` is not exactly 32 bytes.
    """
    if not isinstance(master, (bytes, bytearray)) or len(master) != 32:
        raise ValueError("master key must be 32 bytes")
    from decoy_engine.determinism._hkdf import hkdf_sha256

    return hkdf_sha256(
        ikm=bytes(master),
        salt=b"\x00" * 32,
        info=PACK_SIGNING_INFO.encode("utf-8"),
        length=32,
    )


def sign_pack(pack_dir: Path, key: bytes) -> str:
    """Sign the pack manifest in *pack_dir* in place (install/deploy-time step).

    Reads ``manifest.json``, computes the HMAC-SHA256 over its canonical
    payload via :func:`sign_manifest`, writes the digest back into the
    ``manifest_hmac`` field, and returns the digest. Because the signing key
    is per-instance (Option A), this runs on the target box at install time,
    not in the repository.

    Parameters
    ----------
    pack_dir:
        Directory containing ``manifest.json``.
    key:
        Raw signing key bytes (typically from :func:`derive_pack_signing_key`).

    Returns
    -------
    str
        The 64-character hex HMAC written into the manifest.

    Raises
    ------
    ValueError
        If ``key`` is empty or ``manifest.json`` is missing/corrupt.
    """
    if not key:
        raise ValueError("sign_pack: key must be non-empty bytes")
    manifest_path = pack_dir / "manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = ModelPackManifest.from_dict(raw)
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        raise ValueError(f"sign_pack: cannot read manifest in {pack_dir}: {exc}") from exc
    digest = sign_manifest(manifest, key)
    raw["manifest_hmac"] = digest
    # Write canonically (sort_keys) so the on-disk manifest is stable across signings.
    manifest_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return digest


def _canonical_payload(manifest: ModelPackManifest) -> bytes:
    """Return the deterministic signing payload for *manifest*.

    The ``manifest_hmac`` field is cleared to ``""`` before serialisation so
    the payload is stable regardless of whether the manifest has been signed.
    All other fields are included in signature scope.
    """
    d = manifest.to_dict()
    d["manifest_hmac"] = ""  # exclude the signature field from its own scope
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest(manifest: ModelPackManifest, key: bytes) -> str:
    """Compute and return the HMAC-SHA256 hex signature for *manifest*.

    Does NOT mutate ``manifest``; the caller must store the result in
    ``manifest.manifest_hmac``.

    Parameters
    ----------
    manifest:
        The manifest to sign.  ``manifest.manifest_hmac`` is excluded from
        the payload so this function is idempotent.
    key:
        Raw signing key bytes.  Must be non-empty.

    Returns
    -------
    str
        64-character lower-case hex HMAC-SHA256 digest.

    Raises
    ------
    ValueError
        If ``key`` is empty.
    """
    if not key:
        raise ValueError("sign_manifest: key must be non-empty bytes")
    payload = _canonical_payload(manifest)
    return _hmac_mod.new(key, payload, hashlib.sha256).hexdigest()


def verify_manifest(manifest: ModelPackManifest, key: bytes) -> bool:
    """Verify the HMAC-SHA256 signature stored in ``manifest.manifest_hmac``.

    Uses ``hmac.compare_digest`` for constant-time comparison.

    Parameters
    ----------
    manifest:
        Manifest to verify.  Must have a non-empty ``manifest_hmac`` field.
    key:
        Raw signing key bytes.  Must match the key used in ``sign_manifest``.

    Returns
    -------
    bool
        ``True`` if the stored HMAC matches the expected value for this key.
        ``False`` if the manifest has been tampered with, the key is wrong,
        or ``manifest_hmac`` is empty.
    """
    if not key:
        return False
    stored = manifest.manifest_hmac or ""
    if not stored:
        return False
    expected = sign_manifest(manifest, key)
    # compare_digest guards against timing attacks; both args must be the
    # same type (str here).
    return _hmac_mod.compare_digest(expected, stored)


def load_signing_key_from_env() -> bytes | None:
    """Load the signing key from ``DECOY_PACK_SIGNING_KEY`` env var.

    Returns
    -------
    bytes | None
        32-byte key if the env var is set and valid hex, else ``None``.

    Logs a warning (not an error) if the env var is set but not valid hex,
    so a misconfigured deployment surfaces the issue without crashing.
    """
    raw = os.environ.get(SIGNING_KEY_ENV, "")
    if not raw:
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        _log.warning(
            "%s is set but is not valid hex; pack signature verification disabled. "
            "Expected 64 hex characters (32 bytes).",
            SIGNING_KEY_ENV,
        )
        return None
    if len(key) < 16:
        _log.warning(
            "%s key is only %d bytes; at least 16 bytes recommended.",
            SIGNING_KEY_ENV,
            len(key),
        )
    return key
