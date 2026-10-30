"""The trusted admission choke point: every secret value crosses ``prepare_secret`` once.

It describes, reads once, decodes, validates every selected requirement, derives the runtime
key, and translates any failure into the closed ``SecretProviderError`` hierarchy outside the
``except`` block so no provider/SDK exception survives as ``__cause__`` or ``__context__``.
Raw material is closed on every exit; only an opt-in cache keeps a (cloned) prepared value.

Failures are never re-raised: ``closed_error`` builds a new engine error from the registered code
and it is raised without a traceback from the failing frames, whose locals may hold material
(``traceback.TracebackException(capture_locals=True)`` renders them). Material between the read
and the derivation lives only in ``SecretBuffer`` objects, which render as ``<redacted secret>``.

The same holds for value-bearing OBJECTS (a dataclass provider's repr prints every field): each
public entry point moves its arguments into an ``_OpaqueBox`` (fixed repr), deletes its own
locals, and runs the work in a contained worker whose frames never reach the raised error. The
only frames a traceback keeps are the entry point's (holding the box) and ``_call_closed``.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from decoy_engine.keyprovider import (
    KEY_REQUIRED_GUIDANCE,
    MIN_SECRET_BYTES,
    KeyedStrategyRequiresSecret,
    KeyProvider,
    SeedKeyProvider,
)
from decoy_engine.secret_providers._buffer import SecretBuffer
from decoy_engine.secret_providers._cache import default_cache
from decoy_engine.secret_providers._errors import (
    SecretExpired,
    SecretLeaseUnsupported,
    SecretProviderUnavailable,
    SecretVersionUnavailable,
    closed_error,
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


# The registry admitting the current preflight; part of the cache scope (see _cache.py).
_ADMITTING_REGISTRY: contextvars.ContextVar[SecretProviderRegistry | None] = contextvars.ContextVar(
    "decoy_admitting_registry", default=None
)


def _derive(
    material: SecretBuffer, requirements: tuple[SecretRequirement, ...], key_version: str
) -> SecretBuffer:
    derived: SecretBuffer | None = None
    try:
        for req in requirements:
            out = SecretBuffer.adopt(bytearray(req.prepare(bytes(material.view()), key_version)))
            if derived is None or req.id == MASK_ROOT_V1:
                if derived is not None:
                    derived.close()
                derived = out
            else:
                out.close()
        if derived is None:
            raise SecretProviderUnavailable
        return derived
    except BaseException:
        if derived is not None:
            derived.close()
        raise


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
        material = SecretBuffer.adopt(
            bytearray(decode_material(bytes(resolved.buffer.view()), ref.encoding, requirements[0]))
        )
        with material:
            derived = _derive(material, requirements, descriptor.key_version)
        return PreparedSecret(
            derived,
            key_version=descriptor.key_version,
            requirement_ids=tuple(r.id for r in requirements),
            expires_at=resolved.expires_at,
        )


class _OpaqueBox:
    """Carries value-bearing objects through frames that traceback rendering may capture."""

    __slots__ = ("_items",)

    def __init__(self, *items: object) -> None:
        self._items = items

    def open(self) -> tuple[Any, ...]:
        return self._items

    def __repr__(self) -> str:
        return "<opaque>"

    __str__ = __repr__

    def __reduce_ex__(self, *_: object) -> Any:
        raise TypeError("opaque boxes cannot be pickled")


_T = TypeVar("_T")


def _contained(
    work: Callable[[_OpaqueBox], _T], box: _OpaqueBox
) -> tuple[_T | None, Exception | None]:
    """Run ``work``; its failure leaves only a rebuilt engine error, never its frames."""
    try:
        return work(box), None
    except Exception as exc:
        return None, closed_error(exc)


def _call_closed(work: Callable[[_OpaqueBox], _T], box: _OpaqueBox) -> _T:
    """Raise from a frame whose locals hold no value-bearing object (only the box)."""
    result, failure = _contained(work, box)
    if failure is not None:
        raise failure.with_traceback(None) from None
    if result is None:  # unreachable: a failure raises above
        raise SecretProviderUnavailable
    return result


@dataclass(frozen=True)
class _ContextSnapshot:
    """The provider's security context at the start of a load, as digests only.

    ``key`` is the salted cache-key component; ``digest`` identifies the context for the
    before/after comparison. Neither holds an address, a namespace or a token.
    """

    key: str
    digest: bytes

    def __repr__(self) -> str:
        return "<context snapshot>"


def _security_context(provider: SecretProvider) -> tuple[str, ...]:
    """``cache_context()`` is an optional provider member returning strings that select which
    secret a ref resolves to (a Vault address, namespace and token)."""
    hook = getattr(provider, "cache_context", None)
    context = tuple(hook()) if callable(hook) else ()
    if not all(isinstance(part, str) for part in context):
        raise SecretProviderUnavailable
    return (provider.scheme, *context)


def _digest(context: tuple[str, ...]) -> bytes:
    return hashlib.sha256(json.dumps(context).encode("utf-8")).digest()


def _snapshot(
    provider: SecretProvider, ref: SecretRef, requirements: tuple[SecretRequirement, ...]
) -> _ContextSnapshot:
    context = _security_context(provider)
    return _ContextSnapshot(
        key=ref.get_cache_key_component(tuple(r.id for r in requirements), context=context),
        digest=_digest(context),
    )


def _load_in_context(
    provider: SecretProvider,
    ref: SecretRef,
    requirements: tuple[SecretRequirement, ...],
    clock: Clock,
    snapshot: _ContextSnapshot,
) -> PreparedSecret:
    """One load bound to ``snapshot``: the context must be the same before and after the read.

    A shared mutable client changed mid-load could answer for another tenant than the one the
    cache key names; such a result is closed and never returned or cached.
    """
    if _digest(_security_context(provider)) != snapshot.digest:
        raise SecretProviderUnavailable
    prepared = _load(provider, ref, requirements, clock)
    if _digest(_security_context(provider)) != snapshot.digest:
        prepared.close()
        raise SecretProviderUnavailable
    return prepared


def _prepare_boxed(box: _OpaqueBox) -> PreparedSecret:
    provider, ref, requirements, policy = box.open()
    if not requirements:
        raise SecretProviderUnavailable
    snapshot = _snapshot(provider, ref, requirements)

    def loader() -> PreparedSecret:
        return _load_in_context(provider, ref, requirements, policy.clock, snapshot)

    if policy.ttl_seconds > 0:
        return default_cache().get_or_load(
            snapshot.key,
            owners=(provider, _ADMITTING_REGISTRY.get()),
            ttl=policy.ttl_seconds,
            clock=policy.clock,
            loader=loader,
        )
    return loader()


def prepare_secret(
    *,
    provider: SecretProvider,
    ref: SecretRef,
    requirements: tuple[SecretRequirement, ...],
    cache_policy: SecretCachePolicy | None = None,
) -> PreparedSecret:
    """Admit one pinned secret for ``requirements``. Fails closed with a registered F29 reason."""
    box = _OpaqueBox(provider, ref, requirements, cache_policy or SecretCachePolicy())
    del provider, ref, requirements, cache_policy
    return _call_closed(_prepare_boxed, box)


def _load_legacy(
    key_provider: KeyProvider, requirements: tuple[SecretRequirement, ...]
) -> PreparedSecret:
    if isinstance(key_provider, SeedKeyProvider) or any(r.id != MASK_ROOT_V1 for r in requirements):
        raise KeyedStrategyRequiresSecret(KEY_REQUIRED_GUIDANCE)
    key = SecretBuffer.adopt(bytearray(key_provider.mask_key()))
    if len(key.view()) < MIN_SECRET_BYTES:
        key.close()
        raise KeyedStrategyRequiresSecret(KEY_REQUIRED_GUIDANCE)
    return PreparedSecret(
        key,
        key_version=str(key_provider.key_version),
        requirement_ids=tuple(r.id for r in requirements),
    )


def _legacy_boxed(box: _OpaqueBox) -> PreparedSecret:
    return _load_legacy(*box.open())


def prepare_legacy(
    key_provider: KeyProvider, requirements: tuple[SecretRequirement, ...]
) -> PreparedSecret:
    """Compatibility bridge: a duck-typed ``KeyProvider`` enters the same admission pipeline.

    It has no external ref and no cross-run cache, and satisfies only the mask-root requirement.
    A ``SeedKeyProvider`` (the 8-byte job_seed) is never a secret.
    """
    box = _OpaqueBox(key_provider, requirements)
    del key_provider, requirements
    return _call_closed(_legacy_boxed, box)


@dataclass
class AdmittedKey:
    """The outcome of admission: the provider execution uses, and the owner to close."""

    key_provider: KeyProvider | None
    prepared: PreparedSecret | None = None
    requirements: tuple[SecretRequirement, ...] = ()

    def close(self) -> None:
        if self.prepared is not None:
            self.prepared.close()


class AdmissionScope:
    """Owns the admitted secret of one run so its caller can close it on every exit path."""

    def __init__(self) -> None:
        self.admitted: AdmittedKey | None = None

    def adopt(self, admitted: AdmittedKey) -> AdmittedKey:
        self.admitted = admitted
        return admitted

    def close(self) -> None:
        if self.admitted is not None:
            self.admitted.close()

    def __enter__(self) -> AdmissionScope:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _admit_boxed(box: _OpaqueBox) -> AdmittedKey:
    config, key_provider, registry, cache_policy = box.open()
    requirements = secret_requirements_for_config(config)
    ref_text = (config.get("global_settings") or {}).get("mask_secret_ref")
    if key_provider is not None:
        if not requirements:
            return AdmittedKey(key_provider)
        prepared = _load_legacy(key_provider, requirements)
        return AdmittedKey(prepared.key_provider(), prepared, requirements)
    if not ref_text:
        if requirements:
            raise KeyedStrategyRequiresSecret(KEY_REQUIRED_GUIDANCE)
        return AdmittedKey(None)
    requirements = requirements or (MASK_ROOT,)
    ref = parse_ref(ref_text)
    registry = registry or default_registry()
    provider = registry.get(ref.scheme)
    # ``preflight`` is extension code: its errors cross the same closed boundary.
    scope = _ADMITTING_REGISTRY.set(registry)
    try:
        admitted = provider.preflight(ref, requirements, cache_policy or SecretCachePolicy())
    finally:
        _ADMITTING_REGISTRY.reset(scope)
    return AdmittedKey(admitted.key_provider(), admitted, requirements)


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
    box = _OpaqueBox(config, key_provider, registry, cache_policy)
    del config, key_provider, registry, cache_policy
    return _call_closed(_admit_boxed, box)


def assert_plan_requirements_admitted(plan: Plan, admitted: AdmittedKey) -> None:
    """Late defense in depth: the compiled plan may not need more than admission covered.

    A mismatch is an internal invariant failure raised before masking; it never triggers a
    second fetch. (The config classifier is conservative, so admitted ⊇ plan is the invariant.)
    """
    if secret_requirements_for_plan(plan) and not admitted.requirements:
        admitted.close()
        raise KeyedStrategyRequiresSecret(KEY_REQUIRED_GUIDANCE)


__all__ = [
    "AdmissionScope",
    "AdmittedKey",
    "admit_mask_key",
    "assert_plan_requirements_admitted",
    "prepare_legacy",
    "prepare_secret",
]
