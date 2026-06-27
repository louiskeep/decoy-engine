"""Model-pack loader with security + fallback behaviour (ML2.1).

Implements the ``decoy-model-pack/v1`` loading contract:
  - Validates manifest format, feature-schema version, and SHA-256 of weights
    before calling joblib.load -- the SHA check guards against tampered files.
  - Rejects: missing pack, corrupt pack, wrong-feature-schema pack, signature
    mismatch.
  - Falls back to deterministic detection (regex baseline) on missing or
    corrupt pack with a WARNING log (not a crash).
  - Admin can disable ML entirely via the ``DECOY_ML_DISABLED`` env var;
    engine returns deterministic output unchanged.

Security note on joblib:
    joblib.load is called ONLY after the SHA-256 of the weights file is
    verified against the manifest.  The pack is expected to be a locally
    generated, trusted artifact (§B.5: on-prem only; no cloud inference).
    Verification before loading is the established mitigation for the
    pickle-deserialization risk; see also the warning in trainer.py.

Source: ml2.1-model-pack-loading.md; ml-benchmarking-and-privacy.md §B.7.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from decoy_engine.storm.model_pack.provenance import (
    load_signing_key_from_env,
    verify_manifest,
)
from decoy_engine.storm.model_pack.types import (
    FEATURE_SCHEMA_VERSION,
    PACK_FORMAT,
    ModelPackManifest,
)

_log = logging.getLogger(__name__)

#: Tokens accepted as "on"/"off" for the fail-closed signature flag, case-insensitive.
_REQUIRE_SIG_TRUTHY = frozenset({"1", "true", "yes", "on"})
_REQUIRE_SIG_FALSY = frozenset({"0", "false", "no", "off", ""})


class ModelPackLoadError(ValueError):
    """Raised when a pack fails any validation check."""


def _require_signature_enabled() -> bool:
    """Parse ``DECOY_PACK_REQUIRE_SIGNATURE`` into a bool, failing CLOSED.

    A security flag whose purpose is fail-closed posture must not silently
    fail *open* on a plausible misconfiguration (M1). So ``true``/``yes``/``on``
    (any case) all enable the requirement, the explicit falsy tokens disable
    it, and ANY other value raises ``ModelPackLoadError`` -- an ambiguous
    security flag is treated as "require + you typo'd it", never as "off".
    """
    raw = os.environ.get("DECOY_PACK_REQUIRE_SIGNATURE", "0").strip().lower()
    if raw in _REQUIRE_SIG_TRUTHY:
        return True
    if raw in _REQUIRE_SIG_FALSY:
        return False
    raise ModelPackLoadError(
        f"DECOY_PACK_REQUIRE_SIGNATURE has an unrecognized value {raw!r}; "
        f"expected one of {sorted(_REQUIRE_SIG_TRUTHY)} or {sorted(_REQUIRE_SIG_FALSY)}. "
        "Refusing to guess a security-flag value (fail-closed)."
    )


class ModelPackLoader:
    """Validates and loads a ``decoy-model-pack/v1`` from a directory.

    Parameters
    ----------
    pack_dir:
        Path to the directory containing ``manifest.json`` and
        ``model.joblib``.

    Examples
    --------
    >>> loader = ModelPackLoader(Path("docs/v2/ml/packs/lgbm-v1"))
    >>> pack = loader.load()   # raises ModelPackLoadError on any problem
    >>> pack["clf"].predict_proba(...)
    """

    def __init__(self, pack_dir: Path) -> None:
        self._pack_dir = pack_dir

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_ml_disabled(self) -> bool:
        """True when the ``DECOY_ML_DISABLED`` env var is set to ``"1"``."""
        return os.environ.get("DECOY_ML_DISABLED", "0") == "1"

    def load(self) -> dict[str, Any]:
        """Load and validate the pack.  Returns the pack dict or raises.

        Returns
        -------
        dict with keys:
            "vec"       - fitted DictVectorizer
            "clf"       - CalibratedClassifierCV (LightGBM base)
            "classes"   - list of class-name strings
            "manifest"  - ModelPackManifest

        Raises
        ------
        ModelPackLoadError
            If any check fails: missing files, corrupt weights, wrong feature
            schema version, or SHA-256 mismatch.
        """
        if self.is_ml_disabled:
            raise ModelPackLoadError(
                "ML is disabled via DECOY_ML_DISABLED=1; engine uses deterministic fallback."
            )

        manifest = self._load_manifest()
        self._check_format(manifest)
        self._check_feature_schema(manifest)
        model_bytes = self._read_weights()
        self._check_sha256(model_bytes, manifest)
        self._check_signature(manifest)

        pack_obj = self._deserialise(model_bytes)
        pack_obj["manifest"] = manifest
        return pack_obj

    def load_with_fallback(self) -> dict[str, Any] | None:
        """Try to load the pack; return ``None`` on any failure + log a warning.

        The caller (e.g. a STORM scanner) should treat ``None`` as "use the
        deterministic regex baseline" -- the pack not loading must NEVER crash
        the host process (ml2.1-model-pack-loading.md Done-state requirement).
        """
        try:
            return self.load()
        except ModelPackLoadError as exc:
            _log.warning("Model pack unavailable; falling back to regex baseline: %s", exc)
            return None
        except Exception as exc:
            _log.warning(
                "Unexpected error loading model pack; falling back to regex baseline: %s", exc
            )
            return None

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load_manifest(self) -> ModelPackManifest:
        manifest_path = self._pack_dir / "manifest.json"
        if not manifest_path.exists():
            raise ModelPackLoadError(
                f"Missing manifest.json in {self._pack_dir}. "
                "The pack directory must contain manifest.json and model.joblib."
            )
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            return ModelPackManifest.from_dict(raw)
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            raise ModelPackLoadError(f"Corrupt manifest.json in {self._pack_dir}: {exc}") from exc

    def _check_format(self, manifest: ModelPackManifest) -> None:
        if manifest.format_version != PACK_FORMAT:
            raise ModelPackLoadError(
                f"Unsupported pack format {manifest.format_version!r}; "
                f"this engine expects {PACK_FORMAT!r}."
            )

    def _check_feature_schema(self, manifest: ModelPackManifest) -> None:
        if manifest.feature_schema_version != FEATURE_SCHEMA_VERSION:
            raise ModelPackLoadError(
                f"Feature schema mismatch: pack was trained against "
                f"{manifest.feature_schema_version!r} but the engine produces "
                f"{FEATURE_SCHEMA_VERSION!r}. Re-train the pack against the "
                "current feature schema before loading."
            )

    def _read_weights(self) -> bytes:
        model_path = self._pack_dir / "model.joblib"
        if not model_path.exists():
            raise ModelPackLoadError(f"Missing model.joblib in {self._pack_dir}.")
        try:
            return model_path.read_bytes()
        except OSError as exc:
            raise ModelPackLoadError(
                f"Cannot read model.joblib in {self._pack_dir}: {exc}"
            ) from exc

    def _check_sha256(self, model_bytes: bytes, manifest: ModelPackManifest) -> None:
        if not manifest.sha256:
            raise ModelPackLoadError(
                "Manifest sha256 field is empty; this pack was not properly signed."
            )
        actual = hashlib.sha256(model_bytes).hexdigest()
        if actual != manifest.sha256:
            raise ModelPackLoadError(
                f"model.joblib SHA-256 mismatch: "
                f"expected {manifest.sha256!r}, got {actual!r}. "
                "The weights file may have been tampered with or corrupted."
            )

    def _check_signature(self, manifest: ModelPackManifest) -> None:
        """Verify HMAC-SHA256 signature when a signing key is configured (ML3.2).

        Behaviour:
          - Key configured + valid HMAC stored  -> pass (signature verified).
          - Key configured + HMAC mismatch      -> ModelPackLoadError (tampered).
          - Key configured + no HMAC stored     -> ModelPackLoadError (unsigned pack).
          - No key configured + HMAC stored     -> warn once; verification skipped.
          - No key configured + no HMAC stored  -> warn once; unsigned pack accepted.

        The key is read from the DECOY_PACK_SIGNING_KEY env var (hex-encoded bytes).

        Fail-closed production posture (Option A): when
        ``DECOY_PACK_REQUIRE_SIGNATURE`` is truthy (``1``/``true``/``yes``/``on``,
        any case; see :func:`_require_signature_enabled`), a missing key is
        treated as a hard error rather than a warning, so a misconfigured
        production box (platform forgot to derive/inject the key) refuses to
        load an unverifiable pack instead of silently trusting it. Development
        leaves the flag unset and keeps the warn-and-continue behaviour.

        Note on raising vs falling back (L2): this method RAISES; callers using
        :meth:`load_with_fallback` will catch that and degrade to the regex
        baseline (no unverified ML runs, but the job is not aborted). Callers
        that need a hard stop on a require-signature failure must use
        :meth:`load` directly.

        Note on signature scope (L1): the HMAC binds the typed manifest fields
        only (everything ``ModelPackManifest`` knows about, incl. ``sha256`` and
        ``eval_report_hash``); any extra non-dataclass keys in manifest.json are
        outside signature scope but are never read by the loader, and the
        weights are independently SHA-256 checked.
        """
        key = load_signing_key_from_env()
        signed = bool(manifest.manifest_hmac)
        require_sig = _require_signature_enabled()

        if key is None and require_sig:
            raise ModelPackLoadError(
                f"DECOY_PACK_REQUIRE_SIGNATURE is enabled but no DECOY_PACK_SIGNING_KEY "
                f"is configured; refusing to load pack {self._pack_dir} whose provenance "
                "cannot be verified. Derive the signing key from the instance master "
                "key (derive_pack_signing_key) and set DECOY_PACK_SIGNING_KEY."
            )

        if key is not None:
            # Key is configured: enforce signature.
            if not signed:
                raise ModelPackLoadError(
                    f"Pack {self._pack_dir} is unsigned (manifest_hmac is empty) "
                    "but DECOY_PACK_SIGNING_KEY is configured. "
                    "Sign the pack with sign_manifest() before deploying."
                )
            if not verify_manifest(manifest, key):
                raise ModelPackLoadError(
                    f"Pack {self._pack_dir} signature verification FAILED. "
                    "The manifest may have been tampered with. "
                    "Re-sign with the current key or investigate the discrepancy."
                )
            _log.debug("Pack %s signature verified (ML3.2).", self._pack_dir)
        else:
            # No key configured: accept but flag.
            if signed:
                _log.warning(
                    "Pack %s has a manifest_hmac signature but DECOY_PACK_SIGNING_KEY "
                    "is not configured; signature verification skipped.",
                    self._pack_dir,
                )
            else:
                _log.warning(
                    "Pack %s is unsigned (manifest_hmac empty) and no "
                    "DECOY_PACK_SIGNING_KEY is configured. "
                    "Provenance cannot be verified (ML3.2).",
                    self._pack_dir,
                )

    def _deserialise(self, model_bytes: bytes) -> dict[str, Any]:
        # SHA-256 verified above before reaching this point.
        # joblib.load of a trusted, locally generated, hash-verified artifact.
        try:
            import io

            import joblib  # type: ignore[import]
        except ImportError as exc:
            raise ModelPackLoadError(
                "joblib is required to load a model pack. Install via: pip install -e '.[ml]'"
            ) from exc
        try:
            return dict(joblib.load(io.BytesIO(model_bytes)))
        except Exception as exc:
            raise ModelPackLoadError(f"Failed to deserialise model.joblib: {exc}") from exc
