"""Immutable public types of the ``secret-provider/v1`` contract.

References follow the Vault KV v2 read model (exact ``?version=``), AWS ``VersionId`` / GCP
numeric version / Azure version pinning (moving aliases are never a job pin), and RFC 3986 URI
strictness. See docs/plans/2026-10-29-f13-secret-provider-contract-preflight.md.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast, runtime_checkable
from urllib.parse import parse_qsl, unquote, urlsplit

from decoy_engine import reasons
from decoy_engine.keyprovider import MaskSecretError
from decoy_engine.secret_providers._buffer import SecretBuffer
from decoy_engine.secret_providers._errors import (
    BAD_REF_MESSAGE,
    SecretClosedError,
    SecretProviderUnavailable,
)

API_VERSION: Literal["secret-provider/v1"] = "secret-provider/v1"

# Documented clock-drift margin between this host and a provider's `expires_at`.
EXPIRY_SKEW_SECONDS = 5
MAX_CACHE_TTL_SECONDS = 60
ENCODINGS = ("base64", "hex", "raw")

# High-entropy, per-process, never persisted or logged: cache keys are not brute-forceable
# from low-entropy refs (env names, Vault paths).
_PROCESS_SALT = os.urandom(32)

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_VERSION_RE = re.compile(r"^[1-9][0-9]{0,9}$")
_LOGICAL_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")


def _bad_ref() -> MaskSecretError:
    # Fixed text: the reference (path, field, name) is never repeated in an error.
    return MaskSecretError(code=reasons.runtime.BAD_SECRET_REF, message=BAD_REF_MESSAGE)


@dataclass(frozen=True, repr=False)
class SecretRef:
    """A parsed, canonical secret reference. Never rendered with its locator."""

    scheme: str
    locator: str
    version: str | None = None
    field: str | None = None
    encoding: str | None = None
    key_version: str | None = None

    def __repr__(self) -> str:
        return f"SecretRef(scheme={self.scheme!r})"

    __str__ = __repr__

    def canonical(self) -> str:
        parts = [self.scheme, self.locator]
        parts += [f"{k}={v}" for k, v in sorted(self._params().items())]
        return "\x1f".join(parts)

    def _params(self) -> dict[str, str]:
        raw = {
            "version": self.version,
            "field": self.field,
            "encoding": self.encoding,
            "key_version": self.key_version,
        }
        return {k: v for k, v in raw.items() if v is not None}

    def get_cache_key_component(
        self, requirement_ids: tuple[str, ...] = (), *, context: tuple[str, ...] = ()
    ) -> str:
        """Per-process salted hash of the canonical ref, the requirement set and the provider's
        security context. Neither the raw ref nor the context reaches the cache subsystem."""
        material = json.dumps([self.canonical(), sorted(requirement_ids), list(context)])
        return hmac.new(_PROCESS_SALT, material.encode("utf-8"), hashlib.sha256).hexdigest()


def parse_ref(text: object) -> SecretRef:
    """Parse ``env:NAME[?..]``, ``file:/PATH[?..]`` or ``vault://mount/path?version=N&field=F&encoding=E``.

    Any other well-formed ``scheme:locator[?encoding=..&key_version=..]`` parses with the local
    grammar so the registry, not the parser, owns the "unknown provider" decision. Anything
    malformed is ``bad_secret_ref``.
    """
    if not isinstance(text, str) or not text or "\x00" in text:
        raise _bad_ref()
    match = _SCHEME_RE.match(text)
    if match is None:
        raise _bad_ref()
    scheme = match.group(1).lower()
    if scheme == "vault":
        return _parse_vault(text)
    return _parse_local(scheme, text[match.end() :])


def _encoding(value: str) -> str:
    if value not in ENCODINGS:
        raise _bad_ref()
    return value


def _parse_local(scheme: str, rest: str) -> SecretRef:
    locator, encoding, key_version = rest, None, None
    head, sep, query = rest.rpartition("?")
    if sep and query:
        pairs = (
            parse_qsl(query, keep_blank_values=True, strict_parsing=True) if "=" in query else None
        )
        if pairs and {k for k, _ in pairs} <= {"encoding", "key_version"}:
            keys = [k for k, _ in pairs]
            if len(keys) != len(set(keys)):
                raise _bad_ref()
            locator = head
            for key, value in pairs:
                if key == "encoding":
                    encoding = _encoding(value)
                elif not _LOGICAL_VERSION_RE.match(value):
                    raise _bad_ref()
                else:
                    key_version = value
    if not locator:
        raise _bad_ref()
    return SecretRef(scheme=scheme, locator=locator, encoding=encoding, key_version=key_version)


def _parse_vault(text: str) -> SecretRef:
    try:
        parts = urlsplit(text)
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query))
    except ValueError:
        raise _bad_ref() from None
    if parts.fragment or "@" in parts.netloc or ":" in parts.netloc or not parts.netloc:
        raise _bad_ref()
    keys = [k for k, _ in query]
    if len(keys) != len(set(keys)) or not set(keys) <= {"version", "field", "encoding"}:
        raise _bad_ref()
    params = dict(query)
    version, fld, encoding = params.get("version"), params.get("field"), params.get("encoding")
    if version is None or fld is None or encoding is None:
        raise _bad_ref()
    if not _VERSION_RE.match(version) or not fld:
        raise _bad_ref()
    try:
        decoded = unquote(parts.path, errors="strict")
        mount = unquote(parts.netloc, errors="strict")
        fld = unquote(fld, errors="strict")
    except UnicodeDecodeError:
        raise _bad_ref() from None
    segments = decoded.split("/")[1:]
    if not decoded.startswith("/") or not segments:
        raise _bad_ref()
    for seg in (mount, *segments):
        if seg in ("", ".", "..") or "\\" in seg or any(ord(c) < 32 for c in seg):
            raise _bad_ref()
    return SecretRef(
        scheme="vault",
        locator=mount + "/" + "/".join(segments),
        version=version,
        field=fld,
        encoding=_encoding(encoding),
    )


@dataclass(frozen=True)
class SecretDescriptor:
    """Value-free metadata. Allowlisted fields only: no path, field, length, digest, lease id,
    token, namespace or provider text."""

    scheme: str
    key_version: str
    created_at: float | None = None
    expires_at: float | None = None
    renewable: bool = False
    cache_status: Literal["disabled", "eligible"] = "disabled"
    api_version: Literal["secret-provider/v1"] = API_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "scheme": self.scheme,
            "key_version": self.key_version,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "renewable": self.renewable,
            "cache_status": self.cache_status,
        }


@dataclass(frozen=True, repr=False)
class ResolveContext:
    """The immutable security context ONE provider read is made with.

    The engine takes it once per load, derives the cache key from every field of this same object
    and hands the same object to ``resolve``, which reads with it (never with mutable provider or
    client state), so the key and the read cannot name different tenants. A provider that needs
    selectors (a Vault address, namespace, token) subclasses it as a frozen dataclass with
    ``repr=False``; ``context_key_parts`` rejects anything else. Pattern: an immutable request
    context passed explicitly (Go ``context.Context`` values, AWS SDK per-call config) instead of
    ambient mutable client state.
    """

    scheme: str

    def __repr__(self) -> str:
        return f"ResolveContext(scheme={self.scheme!r})"

    __str__ = __repr__

    def __reduce_ex__(self, *_: object) -> Any:
        raise TypeError("resolve contexts cannot be pickled")


_CONTEXT_SCALARS = (str, bool, int, type(None))


def _immutable_value(value: object) -> bool:
    if type(value) is tuple:
        return all(type(v) in _CONTEXT_SCALARS for v in value)
    return type(value) in _CONTEXT_SCALARS


def context_key_parts(context: object, scheme: str) -> tuple[str, ...]:
    """Every field of ``context`` as cache-key strings, or ``SecretProviderUnavailable``.

    Only an exact frozen dataclass subclass of ``ResolveContext`` whose fields are exact
    immutable scalars (or tuples of them), whose rendering is the base's fixed one and whose
    scheme is the provider's is accepted: such an object cannot change between deriving the key
    and the read, and cannot print a token in a traceback.
    """
    cls = type(context)
    params = cls.__dict__.get("__dataclass_params__")
    if (
        not isinstance(context, ResolveContext)
        or params is None
        or not params.frozen
        or cls.__repr__ is not ResolveContext.__repr__
        or cast(object, cls.__str__) is not ResolveContext.__str__
        or type(context.scheme) is not str
        or context.scheme != scheme
    ):
        raise SecretProviderUnavailable
    parts: list[str] = []
    for f in dataclasses.fields(context):
        value = getattr(context, f.name)
        if not _immutable_value(value):
            raise SecretProviderUnavailable
        parts.append(json.dumps([f.name, value]))
    return tuple(parts)


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall(self) -> float: ...


class SystemClock:
    def monotonic(self) -> float:
        import time

        return time.monotonic()

    def wall(self) -> float:
        import time

        return time.time()


@dataclass(frozen=True)
class SecretCachePolicy:
    """Cross-run process cache policy. TTL 0 (default) disables it; the maximum is 60 seconds."""

    ttl_seconds: int = 0
    clock: Clock = field(default_factory=SystemClock, compare=False)

    def __post_init__(self) -> None:
        ttl = self.ttl_seconds
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, int)
            or not 0 <= ttl <= MAX_CACHE_TTL_SECONDS
        ):
            raise _bad_ref()


class ResolvedSecret:
    """The one provider read: raw (undecoded) bytes plus value-free metadata. Context-managed."""

    __slots__ = ("buffer", "created_at", "expires_at", "key_version")

    def __init__(
        self,
        buffer: SecretBuffer,
        *,
        key_version: str,
        expires_at: float | None = None,
        created_at: float | None = None,
    ) -> None:
        self.buffer = buffer
        self.key_version = key_version
        self.expires_at = expires_at
        self.created_at = created_at

    def close(self) -> None:
        self.buffer.close()

    def __enter__(self) -> ResolvedSecret:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return "ResolvedSecret(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, *_: object) -> Any:
        raise TypeError("secrets cannot be pickled")


class PreparedSecret:
    """Validated, derived key material owned by one run (or by the process cache).

    ``mask_key`` is the 32-byte masking key for ``decoy-mask-root-v1``; other requirements hold
    their validated material. ``key_version`` is non-secret.
    """

    __slots__ = ("_buffer", "expires_at", "key_version", "requirement_ids")

    def __init__(
        self,
        buffer: SecretBuffer,
        *,
        key_version: str,
        requirement_ids: tuple[str, ...],
        expires_at: float | None = None,
    ) -> None:
        self._buffer = buffer
        self.key_version = key_version
        self.requirement_ids = requirement_ids
        self.expires_at = expires_at

    @property
    def closed(self) -> bool:
        return self._buffer.closed

    def mask_key(self) -> bytes:
        return bytes(self._buffer.view())

    def clone(self) -> PreparedSecret:
        return PreparedSecret(
            self._buffer.copy(),
            key_version=self.key_version,
            requirement_ids=self.requirement_ids,
            expires_at=self.expires_at,
        )

    def key_provider(self) -> PreparedKeyProvider:
        return PreparedKeyProvider(self)

    def close(self) -> None:
        self._buffer.close()

    def __enter__(self) -> PreparedSecret:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return "PreparedSecret(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, *_: object) -> Any:
        raise TypeError("secrets cannot be pickled")


class PreparedKeyProvider:
    """A ``KeyProvider`` view of a prepared secret; its lifetime is the prepared secret's."""

    __slots__ = ("_prepared",)

    def __init__(self, prepared: PreparedSecret) -> None:
        self._prepared = prepared

    @property
    def key_version(self) -> str:
        return self._prepared.key_version

    def mask_key(self) -> bytes:
        if self._prepared.closed:
            raise SecretClosedError
        return self._prepared.mask_key()

    def __repr__(self) -> str:
        return f"PreparedKeyProvider(key_version={self.key_version!r})"

    def __reduce_ex__(self, *_: object) -> Any:
        raise TypeError("secrets cannot be pickled")


@runtime_checkable
class SecretProvider(Protocol):
    """``secret-provider/v1``. ``describe`` never reads the value; ``preflight`` delegates to the
    common ``prepare_secret`` choke point.

    ``resolve`` reads with the ``ResolveContext`` it is handed. A provider whose read depends on a
    security context (address, namespace, credentials) fixes it at construction and exposes it
    through an optional ``resolve_context()`` member; one without gets ``ResolveContext(scheme)``.
    """

    api_version: Literal["secret-provider/v1"]
    scheme: str

    def describe(self, ref: SecretRef) -> SecretDescriptor: ...

    def resolve(self, ref: SecretRef, context: ResolveContext) -> ResolvedSecret: ...

    def preflight(
        self,
        ref: SecretRef,
        requirements: tuple[Any, ...],
        cache_policy: SecretCachePolicy,
    ) -> PreparedSecret: ...
