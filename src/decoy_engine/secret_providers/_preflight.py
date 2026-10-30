"""The trusted admission choke point: every secret value crosses ``prepare_secret`` once.

It describes, reads once, decodes, validates every selected requirement, derives the runtime
key, and translates any failure into the closed ``SecretProviderError`` hierarchy outside the
``except`` block so no provider/SDK exception survives as ``__cause__`` or ``__context__``.
Raw material is closed on every exit; only an opt-in cache keeps a (cloned) prepared value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from decoy_engine.keyprovider import (
    MIN_SECRET_BYTES,
    KeyedStrategyRequiresSecret,
    KeyProvider,
    MaskSecretError,
    SeedKeyProvider,
)
from decoy_engine.secret_providers._buffer import SecretBuffer
from decoy_engine.secret_providers._cache import default_cache
from decoy_engine.secret_providers._errors import (
    SecretExpired,
    SecretLeaseUnsupported,
    SecretProviderUnavailable,
    SecretVersionUnavailable,
)
from decoy_engine.secret_providers._registry import SecretProviderRegistry, default_registry
from decoy_engine.secret_providers._requirements import (
    MASK_ROOT,
    MASK_ROOT_V1,
    SecretRequirement,
    decode_material,
    secret_requirements_for_config,
    secret_requirements_for_plan,
)
from decoy_engine.secret_providers._types import (
    Clock,
    PreparedSecret,
    SecretCachePolicy,
    SecretProvider,
    SecretRef,
    parse_ref,
)

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan

_NO_KEY_GUIDANCE = (
    "this plan masks a keyed re-identification surface but no mask secret was admitted. "
    "Supply run(key_provider=...) or global_settings.mask_secret_ref (env:NAME, file:/PATH or "
    "a registered provider reference). No job_seed fallback exists in any release phase; for "
    "local development a throwaway secret is enough: DECOY_MASK_SECRET=$(openssl rand -hex 32)."
)


def _safe(exc: BaseException) -> BaseException:
    """Map any failure to a safe type. Our own errors pass through with their chain severed."""
    if isinstance(exc, MaskSecretError):
        exc.__cause__ = None
        exc.__context__ = None
        exc.__suppress_context__ = True
        return exc
    return SecretProviderUnavailable()


def _load(
    provider: SecretProvider,
    ref: SecretRef,
    requirements: tuple[SecretRequirement, ...],
    clock: Clock,
) -> PreparedSecret:
    descriptor = provider.describe(ref)
    if descriptor.renewable:
        raise SecretLeaseUnsupported
    with provider.resolve(ref) as resolved:
        if resolved.key_version != descriptor.key_version:
            raise SecretVersionUnavailable
        if resolved.expires_at is not None:
            if clock.wall() >= resolved.expires_at:
                raise SecretExpired
        raw = bytes(resolved.buffer.view())
        material = decode_material(raw, ref.encoding, requirements[0])
        derived = bytearray()
        for req in requirements:
            out = req.prepare(material, descriptor.key_version)
            if req.id == MASK_ROOT_V1 or not derived:
                derived = bytearray(out)
        return PreparedSecret(
            SecretBuffer.adopt(derived),
            key_version=descriptor.key_version,
            requirement_ids=tuple(r.id for r in requirements),
            expires_at=resolved.expires_at,
        )


def prepare_secret(
    *,
    provider: SecretProvider,
    ref: SecretRef,
    requirements: tuple[SecretRequirement, ...],
    cache_policy: SecretCachePolicy | None = None,
) -> PreparedSecret:
    """Admit one pinned secret for ``requirements``. Fails closed with a registered F29 reason."""
    policy = cache_policy or SecretCachePolicy()
    failure: BaseException | None = None
    prepared: PreparedSecret | None = None
    try:
        if not requirements:
            raise SecretProviderUnavailable

        def loader() -> PreparedSecret:
            return _load(provider, ref, requirements, policy.clock)

        if policy.ttl_seconds > 0:
            key = f"{provider.scheme}:" + ref.get_cache_key_component(
                tuple(r.id for r in requirements)
            )
            prepared = default_cache().get_or_load(
                key, ttl=policy.ttl_seconds, clock=policy.clock, loader=loader
            )
        else:
            prepared = loader()
    except Exception as exc:
        failure = _safe(exc)
    if failure is not None:
        raise failure
    if prepared is None:  # unreachable: a failure raises above
        raise SecretProviderUnavailable
    return prepared


def prepare_legacy(
    key_provider: KeyProvider, requirements: tuple[SecretRequirement, ...]
) -> PreparedSecret:
    """Compatibility bridge: a duck-typed ``KeyProvider`` enters the same admission pipeline.

    It has no external ref and no cross-run cache, and satisfies only the mask-root requirement.
    A ``SeedKeyProvider`` (the 8-byte job_seed) is never a secret.
    """
    failure: BaseException | None = None
    prepared: PreparedSecret | None = None
    try:
        if isinstance(key_provider, SeedKeyProvider) or any(
            r.id != MASK_ROOT_V1 for r in requirements
        ):
            raise KeyedStrategyRequiresSecret(_NO_KEY_GUIDANCE)
        key = bytearray(key_provider.mask_key())
        if len(key) < MIN_SECRET_BYTES:
            raise KeyedStrategyRequiresSecret(_NO_KEY_GUIDANCE)
        prepared = PreparedSecret(
            SecretBuffer.adopt(key),
            key_version=str(key_provider.key_version),
            requirement_ids=tuple(r.id for r in requirements),
        )
    except Exception as exc:
        failure = _safe(exc)
    if failure is not None:
        raise failure
    if prepared is None:  # unreachable: a failure raises above
        raise SecretProviderUnavailable
    return prepared


@dataclass
class AdmittedKey:
    """The outcome of admission: the provider execution uses, and the owner to close."""

    key_provider: KeyProvider | None
    prepared: PreparedSecret | None = None
    requirements: tuple[SecretRequirement, ...] = ()

    def close(self) -> None:
        if self.prepared is not None:
            self.prepared.close()


def admit_mask_key(
    config: Mapping[str, Any],
    *,
    key_provider: KeyProvider | None = None,
    registry: SecretProviderRegistry | None = None,
    cache_policy: SecretCachePolicy | None = None,
) -> AdmittedKey:
    """Pre-read admission from configuration alone. Call before profiling or iterating a source.

    A programmatic ``key_provider`` wins over ``global_settings.mask_secret_ref``. A configured
    ref is always preflighted (even for an unkeyed plan), preserving the earlier fail-early
    behavior for a broken ref. Keyed work with no admitted secret fails closed in every phase.
    """
    requirements = secret_requirements_for_config(config)
    ref_text = (config.get("global_settings") or {}).get("mask_secret_ref")
    if key_provider is not None:
        if not requirements:
            return AdmittedKey(key_provider)
        prepared = prepare_legacy(key_provider, requirements)
        return AdmittedKey(prepared.key_provider(), prepared, requirements)
    if not ref_text:
        if requirements:
            raise KeyedStrategyRequiresSecret(_NO_KEY_GUIDANCE)
        return AdmittedKey(None)
    requirements = requirements or (MASK_ROOT,)
    ref = parse_ref(ref_text)
    provider = (registry or default_registry()).get(ref.scheme)
    prepared = provider.preflight(ref, requirements, cache_policy or SecretCachePolicy())
    return AdmittedKey(prepared.key_provider(), prepared, requirements)


def assert_plan_requirements_admitted(plan: Plan, admitted: AdmittedKey) -> None:
    """Late defense in depth: the compiled plan may not need more than admission covered.

    A mismatch is an internal invariant failure raised before masking; it never triggers a
    second fetch. (The config classifier is conservative, so admitted ⊇ plan is the invariant.)
    """
    if secret_requirements_for_plan(plan) and not admitted.requirements:
        admitted.close()
        raise KeyedStrategyRequiresSecret(_NO_KEY_GUIDANCE)


__all__ = [
    "AdmittedKey",
    "admit_mask_key",
    "assert_plan_requirements_admitted",
    "prepare_legacy",
    "prepare_secret",
]
