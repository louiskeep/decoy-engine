"""Algorithm requirement registry and the keyed-ness classifiers (one authority).

Material rules belong to the algorithm that consumes the secret, never to a universal length:
Fernet (``cryptography.fernet``: URL-safe base64 of exactly 32 bytes), AES-GCM
(``cryptography...aead.AESGCM``: 16/24/32-byte keys), HMAC-SHA256 (RFC 2104: key at least the
32-byte hash output) and the Decoy mask root (>= 32 raw bytes, HKDF-SHA256 derived per RFC 5869).
Library constructors stay authoritative where they validate. Pattern or entropy scans never
certify strength.

Pre-read classification (``secret_requirements_for_config``) and compiled-plan classification
(``secret_requirements_for_plan``) share ``column_is_keyed``; the config classifier is
conservative: a strategy it does not recognise is KEYED, forcing preflight.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from decoy_engine import reasons
from decoy_engine.keyprovider import MaskSecretError, SecretKeyProvider
from decoy_engine.secret_providers._errors import (
    BAD_ENCODING_MESSAGE,
    NEEDS_ENCODING_MESSAGE,
    SecretMaterialInvalid,
)

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan

MASK_ROOT_V1 = "decoy-mask-root-v1"
FERNET_V1 = "fernet-v1"
HMAC_SHA256_V1 = "hmac-sha256-v1"
AES_GCM_V1 = "aes-gcm-v1"


@dataclass(frozen=True)
class SecretRequirement:
    """One algorithm's material contract. Validators are pure: they never mutate the buffer."""

    id: str
    # Legacy hex-then-base64 text decoding is the default for the mask root only; every other
    # requirement needs an explicit `encoding=` on the ref.
    legacy_decoding: bool
    prepare: Callable[[bytes, str], bytes]

    def __repr__(self) -> str:
        return f"SecretRequirement({self.id!r})"


def _prepare_mask_root(material: bytes, key_version: str) -> bytes:
    # SecretKeyProvider owns the >=32 rule (weak_mask_secret) and the HKDF derivation.
    return SecretKeyProvider(material, key_version=key_version).mask_key()


def _prepare_fernet(material: bytes, _key_version: str) -> bytes:
    from cryptography.fernet import Fernet

    text = material.strip()
    try:
        Fernet(text)
    except Exception:  # library validation is authoritative; its text is never surfaced
        raise SecretMaterialInvalid from None
    return text


def _prepare_hmac(material: bytes, _key_version: str) -> bytes:
    if len(material) < 32:
        raise SecretMaterialInvalid
    return material


def _prepare_aes_gcm(material: bytes, _key_version: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(material) not in (16, 24, 32):
        raise SecretMaterialInvalid
    try:
        AESGCM(material)
    except Exception:
        raise SecretMaterialInvalid from None
    return material


REQUIREMENTS: Mapping[str, SecretRequirement] = {
    r.id: r
    for r in (
        SecretRequirement(MASK_ROOT_V1, True, _prepare_mask_root),
        SecretRequirement(FERNET_V1, False, _prepare_fernet),
        SecretRequirement(HMAC_SHA256_V1, False, _prepare_hmac),
        SecretRequirement(AES_GCM_V1, False, _prepare_aes_gcm),
    )
}
MASK_ROOT = REQUIREMENTS[MASK_ROOT_V1]


def _bad_encoding_for(req: SecretRequirement) -> Exception:
    if req.id == MASK_ROOT_V1:
        return MaskSecretError(code=reasons.runtime.BAD_SECRET_REF, message=BAD_ENCODING_MESSAGE)
    return SecretMaterialInvalid()


def decode_material(raw: bytes, encoding: str | None, req: SecretRequirement) -> bytes:
    """Decode provider bytes per the ref's explicit encoding, or the legacy rule (mask root only)."""
    if encoding is None:
        if not req.legacy_decoding:
            raise MaskSecretError(
                code=reasons.runtime.BAD_SECRET_REF, message=NEEDS_ENCODING_MESSAGE
            )
        return _decode_legacy(raw, req)
    if encoding == "raw":
        return raw
    try:
        text = raw.decode("ascii")
        stripped = "".join(text.split())
        if not stripped:
            raise ValueError
        if encoding == "hex":
            return bytes.fromhex(stripped)
        return base64.b64decode(stripped, validate=True)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise _bad_encoding_for(req) from None


def _decode_legacy(raw: bytes, req: SecretRequirement) -> bytes:
    """Hex first (64 hex chars are unambiguously 32 bytes), base64 as the fallback."""
    try:
        stripped = "".join(raw.decode("utf-8").split())
        if not stripped:
            raise ValueError
    except (UnicodeDecodeError, ValueError):
        raise _bad_encoding_for(req) from None
    try:
        return bytes.fromhex(stripped)
    except ValueError:
        pass
    try:
        return base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError):
        raise _bad_encoding_for(req) from None


# --- classification ------------------------------------------------------------------------

# Re-identification surface regardless of the `deterministic` flag (kept in lockstep with the
# keyed-surface map in docs/discussions/2026-07-14-de02-keyprovider-design.md). `tokenize` is
# keyed unconditionally: a seed-derived token is dictionary-attackable without vault access.
ALWAYS_KEYED_STRATEGIES = frozenset(
    {
        "fpe",
        "hash",
        "date_shift",
        "shuffle",
        "bucket_perturb",
        "text_mask",
        "code_set",
        "grouped_series",
        "windowed_date",
        "group_key",
        "joint_mask",
        "tokenize",
    }
)
# Strategies the config-only classifier positively knows are unkeyed absent `deterministic` /
# `vault`. Anything not in either set is treated as KEYED (conservative default).
KNOWN_UNKEYED_STRATEGIES = frozenset(
    {
        "bucketize",
        "categorical",
        "derived",
        "derived_aggregate",
        "faker",
        "formula",
        "from_parent",
        "geo_generalize",
        "passthrough",
        "redact",
        "text_redact",
        "top_code",
        "truncate",
    }
)


def column_is_keyed(
    strategy: Any, *, deterministic: bool, vault: bool, provider_config: Mapping[str, Any]
) -> bool:
    """Shared per-column predicate. A `vault: true` column is always keyed (its mapping is
    persisted, encrypted, for reversal); a nested wrapper is keyed if its child is."""
    if vault or deterministic or strategy in ALWAYS_KEYED_STRATEGIES:
        return True
    if strategy == "nested":
        child = provider_config.get("strategy")
        child_cfg = provider_config.get("strategy_config")
        child_det = bool(child_cfg.get("deterministic")) if isinstance(child_cfg, dict) else False
        # nested-of-nested is not a shipped pattern; classify it keyed rather than exempt it.
        return (
            child == "nested"
            or child in ALWAYS_KEYED_STRATEGIES
            or child_det
            or (child not in KNOWN_UNKEYED_STRATEGIES)
        )
    return strategy not in KNOWN_UNKEYED_STRATEGIES


def secret_requirements_for_config(config: Mapping[str, Any]) -> tuple[SecretRequirement, ...]:
    """Keyed-ness from configuration alone: no schema, no rows, no profiling."""
    for table in config.get("tables") or []:
        if not isinstance(table, dict) or table.get("generate_columns"):
            continue
        for col in table.get("columns") or []:
            if not isinstance(col, dict):
                continue
            pcfg = col.get("provider_config")
            if column_is_keyed(
                col.get("strategy"),
                deterministic=bool(col.get("deterministic")) or bool(col.get("allow_collisions")),
                vault=bool(col.get("vault")),
                provider_config=pcfg if isinstance(pcfg, dict) else {},
            ):
                return (MASK_ROOT,)
    for rel in config.get("relationships") or []:
        parent = rel.get("parent") if isinstance(rel, dict) else None
        if not isinstance(parent, dict) or len(parent.get("columns") or []) > 1:
            return (MASK_ROOT,)
    return ()


def secret_requirements_for_plan(plan: Plan) -> tuple[SecretRequirement, ...]:
    """Keyed-ness of a compiled plan (any keyed column, or any composite-FK group)."""
    for _table, table_seed in plan.seed_envelope.per_table:
        if table_seed.per_group:
            return (MASK_ROOT,)
        for _col, seed in table_seed.per_column:
            if column_is_keyed(
                seed.strategy,
                deterministic=bool(seed.deterministic),
                vault=bool(getattr(seed, "vault", False)),
                provider_config=dict(seed.provider_config),
            ):
                return (MASK_ROOT,)
    return ()
