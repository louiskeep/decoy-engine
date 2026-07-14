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

Secret source (Option A/B split at the seam): the engine only ever accepts
bytes. It ships `SecretKeyProvider` + a `mask_secret_ref` resolver supporting two
ref kinds -- `env:NAME` and `file:/PATH` -- decoding hex OR base64 to raw bytes.
The reference lives in config; the raw secret is NEVER serialized into the plan,
logged, or printed. The platform feeds per-tenant bytes straight into
`run(key_provider=...)`; the engine ships no KMS client or tenancy concept.

We do not roll our own crypto: HKDF-SHA256 (RFC 5869) over the same stdlib
`hmac`/`hashlib` primitive the rest of the engine uses. References:
- RFC 5869 (HKDF): https://datatracker.ietf.org/doc/html/rfc5869
- RFC 2104 (HMAC): https://datatracker.ietf.org/doc/html/rfc2104
- NIST SP 800-57 Pt.1 Rev.5 (256-bit strength target)
- OWASP Key Management ("never derive secrets from low-entropy config")
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from decoy_engine.determinism._hkdf import hkdf_sha256
from decoy_engine.release import is_pre_ga

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


class MaskSecretError(Exception):
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
        super().__init__(code="keyed_strategy_requires_secret", message=message)


class MissingMaskSecret(MaskSecretError):  # noqa: N818 -- design-named gate error (subclasses MaskSecretError).
    """Raised when a `mask_secret_ref` cannot be resolved to any bytes."""

    def __init__(self, message: str = "") -> None:
        super().__init__(code="missing_mask_secret", message=message)


class WeakMaskSecret(MaskSecretError):  # noqa: N818 -- design-named gate error (subclasses MaskSecretError).
    """Raised when a resolved mask secret is shorter than 32 bytes."""

    def __init__(self, message: str = "") -> None:
        super().__init__(code="weak_mask_secret", message=message)


@runtime_checkable
class KeyProvider(Protocol):
    """Opaque source of keyed mask material. The engine only ever takes bytes.

    `key_version` is NON-secret (e.g. "v1"); it is stamped into the evidence
    manifest so a keyed artifact records which key era produced it. `mask_key()`
    returns the mask-root IKM fed at the one substituted slot.

    `key_version` is declared read-only (a property) so a frozen-dataclass
    implementer -- whose field is immutable -- structurally satisfies it.
    """

    @property
    def key_version(self) -> str: ...

    def mask_key(self) -> bytes: ...


@dataclass(frozen=True)
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
                code="bad_secret_ref",
                message=f"secret must be bytes; got {type(self.secret).__name__}.",
            )
        if len(self.secret) < MIN_SECRET_BYTES:
            raise WeakMaskSecret(
                f"mask secret must be at least {MIN_SECRET_BYTES} bytes; got {len(self.secret)}."
            )

    def mask_key(self) -> bytes:
        return hkdf_sha256(
            ikm=bytes(self.secret),
            salt=_KEYPROVIDER_SALT,
            info=f"decoy/mask/{self.key_version}".encode(),
            length=32,
        )


@dataclass(frozen=True)
class SeedKeyProvider:
    """Pre-GA / no-secret path: mask_key IS the 8-byte job_seed.

    Byte-identical IKM to pre-DE-02, so the golden gate stays green. `key_version`
    is "seed" to mark a non-secret-keyed artifact in the manifest.
    """

    job_seed: bytes
    key_version: str = "seed"

    def mask_key(self) -> bytes:
        return self.job_seed


def _decode_secret_material(text: str) -> bytes:
    """Decode a hex OR base64 secret string to raw bytes.

    Hex is tried first (a 64-char hex string is unambiguously 32 bytes); base64
    is the fallback. A value that is valid as both (all-hex, even length) decodes
    as hex -- documented precedence, not ambiguity in practice.
    """
    stripped = "".join(text.split())
    if not stripped:
        raise MaskSecretError(code="bad_secret_ref", message="secret material is empty.")
    try:
        return bytes.fromhex(stripped)
    except ValueError:
        pass
    try:
        return base64.b64decode(stripped, validate=True)
    except (base64.binascii.Error, ValueError) as exc:  # type: ignore[attr-defined]
        raise MaskSecretError(
            code="bad_secret_ref",
            message="secret material is neither valid hex nor base64.",
        ) from exc


def resolve_mask_secret_ref(ref: str) -> bytes:
    """Resolve a `mask_secret_ref` (`env:NAME` or `file:/PATH`) to raw bytes.

    The ref is a REFERENCE, never the secret itself. Supported kinds:
      - `env:NAME`   -> os.environ["NAME"], hex/base64-decoded.
      - `file:/PATH` -> the file's text contents, hex/base64-decoded.

    Raises MissingMaskSecret when the source is absent/unreadable and
    MaskSecretError(code='bad_secret_ref') for an unknown kind.
    """
    if ref.startswith("env:"):
        name = ref[len("env:") :]
        if not name:
            raise MaskSecretError(code="bad_secret_ref", message="env ref has no variable name.")
        raw = os.environ.get(name)
        if raw is None:
            raise MissingMaskSecret(
                f"environment variable {name!r} (from mask_secret_ref) is unset."
            )
        return _decode_secret_material(raw)
    if ref.startswith("file:"):
        path = ref[len("file:") :]
        if not path:
            raise MaskSecretError(code="bad_secret_ref", message="file ref has no path.")
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as exc:
            raise MissingMaskSecret(
                f"mask secret file {path!r} (from mask_secret_ref) could not be read: {exc}."
            ) from exc
        return _decode_secret_material(raw)
    raise MaskSecretError(
        code="bad_secret_ref",
        message=f"mask_secret_ref must start with 'env:' or 'file:'; got {ref!r}.",
    )


def key_provider_from_ref(ref: str, *, key_version: str = "v1") -> SecretKeyProvider:
    """Build a SecretKeyProvider from a `mask_secret_ref`. Validates >=32 bytes."""
    return SecretKeyProvider(resolve_mask_secret_ref(ref), key_version=key_version)


# Strategies whose handlers always feed the mask key (re-identification surface
# regardless of the deterministic flag). Faker/categorical/composite are keyed
# only in deterministic mode (a real source maps to a synthetic value); that is
# caught by the `deterministic` check below, not this set. Kept in lockstep with
# the 23-site keyed-surface map in the DE-02 design doc.
_ALWAYS_KEYED_STRATEGIES = frozenset(
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
    }
)


def plan_has_keyed_strategy(plan: Plan) -> bool:
    """True if the compiled plan contains any keyed re-identification strategy.

    Keyed = any always-keyed strategy, OR any deterministic column (a
    deterministic faker/categorical/composite maps a real source value to a
    synthetic one and is reversible under the key), OR any composite-FK group
    (a keyed pseudonym mapping over real FK values).
    """
    for _table, table_seed in plan.seed_envelope.per_table:
        for _col, col_seed in table_seed.per_column:
            if col_seed.strategy in _ALWAYS_KEYED_STRATEGIES or col_seed.deterministic:
                return True
        if table_seed.per_group:
            return True
    return False


def resolve_key_provider(
    *,
    plan: Plan,
    key_provider: KeyProvider | None = None,
    mask_secret_ref: str | None = None,
) -> KeyProvider | None:
    """Apply the fail-closed gate and return the KeyProvider to thread, or None.

    The single funnel `run_pipeline` calls once per job; the returned provider is
    threaded (never re-gated) into every execution route. None means "no secret,
    use `job_seed`" -- consumers resolve `provider.mask_key() if provider else
    job_seed`.

    Gate (keyed on `is_pre_ga()`, provenance-blind -- presence + length only):
      - A programmatic `key_provider` wins over `mask_secret_ref`.
      - A `mask_secret_ref` resolves to a SecretKeyProvider (which validates
        >=32 bytes at construction -- WeakMaskSecret always enforced).
      - Non-keyed plan: returns the provider unchanged (inert; the mask key is
        never consumed) so a supplied secret is still honored where it happens to
        be used, and None otherwise.
      - Keyed plan + provider present: returns it.
      - Keyed plan + no provider + pre-GA: returns None (falls back to `job_seed`,
        identical to pre-DE-02 -> golden gate stays green).
      - Keyed plan + no provider + GA: raises KeyedStrategyRequiresSecret.
    """
    provider: KeyProvider | None = key_provider
    if provider is None and mask_secret_ref:
        provider = key_provider_from_ref(mask_secret_ref)

    if not plan_has_keyed_strategy(plan):
        return provider

    if provider is not None:
        return provider

    if is_pre_ga():
        return None

    raise KeyedStrategyRequiresSecret(
        "this plan masks a keyed re-identification surface but no mask secret was "
        "supplied. At GA a keyed job must be given a >=32-byte secret via "
        "run(key_provider=...) or global_settings.mask_secret_ref "
        "(env:NAME or file:/PATH). For local dev, a throwaway secret is enough: "
        "DECOY_MASK_SECRET=$(openssl rand -hex 32), then "
        "mask_secret_ref: 'env:DECOY_MASK_SECRET'."
    )


def mask_key_from_provider(provider: KeyProvider | None, job_seed: bytes) -> bytes:
    """The one-slot IKM: the provider's mask root, or `job_seed` when absent."""
    return provider.mask_key() if provider is not None else job_seed


def resolve_mask_key(
    *,
    plan: Plan,
    key_provider: KeyProvider | None = None,
    mask_secret_ref: str | None = None,
) -> bytes:
    """Gate + resolve straight to `mask_key` bytes (for direct-bytes callers such
    as `vault` / `unmask` that stand outside `StrategyContext`)."""
    provider = resolve_key_provider(
        plan=plan, key_provider=key_provider, mask_secret_ref=mask_secret_ref
    )
    return mask_key_from_provider(provider, plan.seed_envelope.job_seed)
