"""KeyProvider: the keyed-mask secret boundary (DE-02, crypto Sprint 4).

The engine's confidentiality secret for every re-identification-protecting
derivation. Before DE-02 the 8-byte `job_seed` doubled as both the
generation-reproducibility seed AND the mask key; DE-02 demotes `job_seed` to
generation-only and moves the key role to a `KeyProvider`.

Design (docs/discussions/2026-07-14-de02-keyprovider-design.md): one-slot IKM
substitution. Everywhere the keyed surface fed `job_seed` as the seed/IKM it now
feeds a `mask_key`:

    mask_key = job_seed                              (no secret; byte-identical to pre-DE-02)
    mask_key = HKDF-SHA256(secret, purpose/version)  (a real >=32-byte secret is present)

Because `determinism.derive()` already domain-separates by namespace (per column)
and by the per-purpose label / column tweak, a single 32-byte `mask_root`
inherits every existing separation for free. Rotating `key_version`
deterministically re-keys the whole surface without touching any derivation code.

Secret source: the engine only ever accepts bytes. A `mask_secret_ref` in config is a
REFERENCE resolved by the versioned `secret_providers` contract (F13): admission runs before
any source read, per-algorithm validation, no `job_seed` fallback in any release phase. The raw
secret is NEVER serialized into the plan, logged, or printed. The platform feeds per-tenant
bytes into `run(key_provider=...)`; the engine ships no tenancy concept.

We do not roll our own crypto: HKDF-SHA256 (RFC 5869) over the same stdlib
`hmac`/`hashlib` primitive the rest of the engine uses. References:
- RFC 5869 (HKDF): https://datatracker.ietf.org/doc/html/rfc5869
- RFC 2104 (HMAC): https://datatracker.ietf.org/doc/html/rfc2104
- NIST SP 800-57 Pt.1 Rev.5 (256-bit strength target)
- OWASP Key Management ("never derive secrets from low-entropy config")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from decoy_engine import reasons
from decoy_engine.determinism._hkdf import hkdf_sha256
from decoy_engine.errors import DecoyError

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan

# HKDF salt for the mask-root derivation. Binds "this is the DE-02 keyprovider
# purpose"; distinct from the determinism layer's own salt so the two roots
# cannot collide. Bumping the "/v1" suffix is a whole-surface re-key event.
_KEYPROVIDER_SALT = b"decoy-engine/keyprovider/v1"

# Minimum raw-secret strength (NIST SP 800-57: 256-bit target). Enforced at
# SecretKeyProvider construction so a weak secret never reaches a keyed
# derivation, pre-GA or GA.
MIN_SECRET_BYTES = 32


class MaskSecretError(DecoyError):
    """A keyed-mask secret was required but absent, unresolvable, or too weak.

    Kwargs-only constructor mirroring `DeterminismError` so callers can
    `except MaskSecretError as e: e.code` consistently.

    Codes:
        keyed_strategy_requires_secret: a keyed plan ran at GA with no secret.
        missing_mask_secret:            a `mask_secret_ref` pointed at an
                                        absent env var / unreadable file.
        weak_mask_secret:               the resolved secret is < 32 bytes.
        bad_secret_ref:                 the ref kind is unknown or the material
                                        is neither valid hex nor base64.
    """

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


class KeyedStrategyRequiresSecret(MaskSecretError):  # noqa: N818 -- design-named gate error (subclasses MaskSecretError).
    """Raised at GA when a plan with a keyed strategy has no mask secret."""

    def __init__(self, message: str = "") -> None:
        super().__init__(code=reasons.runtime.KEYED_STRATEGY_REQUIRES_SECRET, message=message)


class MissingMaskSecret(MaskSecretError):  # noqa: N818 -- design-named gate error (subclasses MaskSecretError).
    """Raised when a `mask_secret_ref` cannot be resolved to any bytes."""

    def __init__(self, message: str = "") -> None:
        super().__init__(code=reasons.runtime.MISSING_MASK_SECRET, message=message)


class WeakMaskSecret(MaskSecretError):  # noqa: N818 -- design-named gate error (subclasses MaskSecretError).
    """Raised when a resolved mask secret is shorter than 32 bytes."""

    def __init__(self, message: str = "") -> None:
        super().__init__(code=reasons.runtime.WEAK_MASK_SECRET, message=message)


@runtime_checkable
class KeyProvider(Protocol):
    """Opaque source of keyed mask material. The engine only ever takes bytes.

    `key_version` is NON-secret (e.g. "v1"); it is a safe-to-record identifier
    for which key era produced an artifact. Recording it into an evidence
    manifest is a platform-layer concern, not an engine guarantee: the engine
    emits no evidence manifest itself, and the commercial platform's evidence
    assembler is what stamps `provider.key_version` onto a job. `mask_key()`
    returns the mask-root IKM fed at the one substituted slot.

    `key_version` is declared read-only (a property) so a frozen-dataclass
    implementer -- whose field is immutable -- structurally satisfies it.
    """

    @property
    def key_version(self) -> str: ...

    def mask_key(self) -> bytes: ...


@dataclass(frozen=True, repr=False)
class SecretKeyProvider:
    """GA path: a managed, opaque >=32-byte secret. Derives a versioned root.

    The raw secret is validated (>=32 bytes) at construction -- the WeakMaskSecret
    gate is always enforced, pre-GA or GA, so a weak secret can never reach a
    keyed derivation. `mask_key()` returns HKDF-SHA256(secret) so the raw secret
    is never itself used as an HMAC key, and rotating `key_version` re-keys the
    whole surface deterministically.
    """

    secret: bytes
    key_version: str = "v1"

    def __post_init__(self) -> None:
        if not isinstance(self.secret, (bytes, bytearray)):
            raise MaskSecretError(
                code=reasons.runtime.BAD_SECRET_REF,
                message=f"secret must be bytes; got {type(self.secret).__name__}.",
            )
        if len(self.secret) < MIN_SECRET_BYTES:
            raise WeakMaskSecret(
                f"mask secret must be at least {MIN_SECRET_BYTES} bytes; got {len(self.secret)}."
            )

    def __repr__(self) -> str:
        # DE-02 (Codex MEDIUM 7): NEVER render the raw secret bytes in a repr --
        # they leak into logs / tracebacks / debuggers. Show only length + version.
        return f"SecretKeyProvider(secret=<redacted {len(self.secret)} bytes>, key_version={self.key_version!r})"

    def mask_key(self) -> bytes:
        return hkdf_sha256(
            ikm=bytes(self.secret),
            salt=_KEYPROVIDER_SALT,
            info=f"decoy/mask/{self.key_version}".encode(),
            length=32,
        )


@dataclass(frozen=True, repr=False)
class SeedKeyProvider:
    """Pre-GA / no-secret path: mask_key IS the 8-byte job_seed.

    Byte-identical IKM to pre-DE-02, so the golden gate stays green. `key_version`
    is "seed" to mark a non-secret-keyed artifact in the manifest.
    """

    job_seed: bytes
    key_version: str = "seed"

    def __repr__(self) -> str:
        # DE-02 (Codex MEDIUM 7): redact the key material even for the seed path.
        return f"SeedKeyProvider(job_seed=<redacted {len(self.job_seed)} bytes>, key_version={self.key_version!r})"

    def mask_key(self) -> bytes:
        return self.job_seed


def key_provider_from_ref(ref: str, *, key_version: str | None = None) -> KeyProvider:
    """Admit a `mask_secret_ref` through the secret-provider choke point.

    Returns a `KeyProvider` view of the prepared secret; the caller owns its lifetime (nothing
    closes it automatically and nothing caches it). Prefer
    `decoy_engine.secret_providers.admit_mask_key`, which also owns closing.
    """
    from decoy_engine.secret_providers import (
        MASK_ROOT_V1,
        REQUIREMENTS,
        SecretCachePolicy,
        default_registry,
        parse_ref,
    )

    parsed = parse_ref(ref)
    if key_version is not None:
        from dataclasses import replace

        parsed = replace(parsed, key_version=key_version)
    provider = default_registry().get(parsed.scheme)
    return provider.preflight(
        parsed, (REQUIREMENTS[MASK_ROOT_V1],), SecretCachePolicy()
    ).key_provider()


def plan_has_keyed_strategy(plan: Plan) -> bool:
    """True if the compiled plan contains any keyed re-identification strategy.

    The rules live in one registry (`secret_providers._requirements`) shared with the
    pre-read config classifier, so the two cannot drift.
    """
    from decoy_engine.secret_providers import secret_requirements_for_plan

    return bool(secret_requirements_for_plan(plan))


KEY_REQUIRED_GUIDANCE = (
    "this plan masks a keyed re-identification surface but no mask secret was admitted. "
    "Supply run(key_provider=...) or global_settings.mask_secret_ref (env:NAME, file:/PATH or "
    "a registered provider reference). No job_seed fallback exists in any release phase; for "
    "local development a throwaway secret is enough: DECOY_MASK_SECRET=$(openssl rand -hex 32)."
)


def require_mask_key(plan: Plan, key_provider: KeyProvider | None) -> bytes:
    """Defense-in-depth gate at every masking choke point: gate on (plan, provider), return mask_key.

    The entry points admit a secret BEFORE reading any source (`secret_providers.admit_mask_key`);
    this re-asserts the invariant for direct callers of the adapters. Keyed work never runs off
    `job_seed` in any release phase: a missing provider, a `SeedKeyProvider`, or a resolved key
    shorter than the 32-byte mask root all fail closed with KeyedStrategyRequiresSecret.
    Non-keyed plans: the mask key is inert -> provider root, else `job_seed`.
    """
    job_seed = plan.seed_envelope.job_seed
    if not plan_has_keyed_strategy(plan):
        return key_provider.mask_key() if key_provider is not None else job_seed
    if key_provider is None or isinstance(key_provider, SeedKeyProvider):
        raise KeyedStrategyRequiresSecret(KEY_REQUIRED_GUIDANCE)
    mask_key = key_provider.mask_key()
    if len(mask_key) < MIN_SECRET_BYTES:
        raise KeyedStrategyRequiresSecret(KEY_REQUIRED_GUIDANCE)
    return mask_key


def mask_key_from_provider(provider: KeyProvider | None, job_seed: bytes) -> bytes:
    """The one-slot IKM: the provider's mask root, or `job_seed` when absent.

    Ungated -- callers that need the fail-closed gate use `require_mask_key`.
    """
    return provider.mask_key() if provider is not None else job_seed


def resolve_mask_key(*, plan: Plan, key_provider: KeyProvider | None = None) -> bytes:
    """Gate + resolve straight to `mask_key` bytes for callers that stand outside
    `StrategyContext`. The provider must already be admitted; refs are not resolved here."""
    return require_mask_key(plan, key_provider)
