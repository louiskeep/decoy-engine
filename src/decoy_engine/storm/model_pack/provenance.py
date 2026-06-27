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

Key source (ESCALATED -- do not configure here):
    The production key source is undecided; see ``keyMgmtNote`` in the
    Sprint C hand-off.  At runtime the loader reads the key from the
    ``DECOY_PACK_SIGNING_KEY`` env var (hex-encoded 32 bytes).

References:
    RFC 2104 (HMAC): https://datatracker.ietf.org/doc/html/rfc2104
    Python stdlib ``hmac``: https://docs.python.org/3/library/hmac.html
"""

from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import json
import logging
import os

from decoy_engine.storm.model_pack.types import ModelPackManifest

_log = logging.getLogger(__name__)

#: Environment variable name for the hex-encoded pack-signing key (32 bytes = 64 hex chars).
SIGNING_KEY_ENV = "DECOY_PACK_SIGNING_KEY"


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
