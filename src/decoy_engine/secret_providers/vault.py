"""HashiCorp Vault KV v2 adapter for the ``secret-provider/v1`` contract (optional extra).

Pattern sources: the Vault KV v2 read API (``GET /:mount/data/:path?version=N`` returns data and
version metadata in ONE response) and the ``hvac`` client's ``read_secret_version``. The adapter
takes a caller-injected, already-authenticated client: authentication, namespace selection, token
renewal and authorization (F45) belong to the caller. ``hvac`` is imported lazily and is never a
base dependency (``pip install decoy-engine[hashicorp-vault]``).

Contract notes:
  * ``describe`` makes no provider call; ``resolve`` is the single round trip (data + metadata
    together), and the returned version must equal the pinned version.
  * Static KV v2 values only. A response carrying a lease id, a renewable flag, a lease duration,
    wrap info or auth data is a dynamic/leased secret and fails ``secret_lease_unsupported``.
  * Cache entries are scoped to this provider instance and its client's address, namespace and
    token (``cache_context``), so tenants sharing a mount path never share a prepared key.
  * Every SDK/transport failure is mapped to a fixed registered reason by exception TYPE; vendor
    text, paths and response bodies never leave this module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Literal

from decoy_engine.errors import DecoyError
from decoy_engine.secret_providers._buffer import SecretBuffer
from decoy_engine.secret_providers._errors import (
    SecretAccessDenied,
    SecretExpired,
    SecretLeaseUnsupported,
    SecretMaterialInvalid,
    SecretProviderUnavailable,
    SecretVersionUnavailable,
)
from decoy_engine.secret_providers._types import (
    API_VERSION,
    PreparedSecret,
    ResolvedSecret,
    SecretCachePolicy,
    SecretDescriptor,
    SecretRef,
    SystemClock,
)

_TRUTHY = frozenset({"1", "true", "t", "yes", "y", "on"})


class VaultClientConfigError(DecoyError):
    """The Vault client could not be built safely from the environment (fixed, value-free text)."""


def _hvac() -> Any:
    try:
        import hvac
    except ImportError:
        raise SecretProviderUnavailable from None
    return hvac


def client_from_env(
    environ: Mapping[str, str] | None = None, *, insecure_skip_tls_verify: bool = False
) -> Any:
    """Build an ``hvac.Client`` from the standard Vault environment variables.

    Honors ``VAULT_ADDR``, ``VAULT_TOKEN``, ``VAULT_NAMESPACE``, ``VAULT_CACERT``,
    ``VAULT_CLIENT_CERT`` and ``VAULT_CLIENT_KEY``. ``VAULT_SKIP_VERIFY`` set truthy is REJECTED,
    never warned about: disabling TLS verification is an in-code decision
    (``insecure_skip_tls_verify=True``), not an environment toggle that can silently enable a
    man-in-the-middle on a privacy product.
    """
    env = os.environ if environ is None else environ
    if str(env.get("VAULT_SKIP_VERIFY", "")).strip().lower() in _TRUTHY:
        raise VaultClientConfigError(
            "VAULT_SKIP_VERIFY is not honored: disabling TLS verification requires an explicit "
            "in-code override (insecure_skip_tls_verify=True)."
        )
    addr = env.get("VAULT_ADDR")
    if not addr:
        raise VaultClientConfigError("VAULT_ADDR is required to build a Vault client.")
    verify: bool | str = False if insecure_skip_tls_verify else (env.get("VAULT_CACERT") or True)
    cert_file, key_file = env.get("VAULT_CLIENT_CERT"), env.get("VAULT_CLIENT_KEY")
    if bool(cert_file) != bool(key_file):
        raise VaultClientConfigError("VAULT_CLIENT_CERT and VAULT_CLIENT_KEY must be set together.")
    return _hvac().Client(
        url=addr,
        token=env.get("VAULT_TOKEN"),
        namespace=env.get("VAULT_NAMESPACE") or None,
        verify=verify,
        cert=(cert_file, key_file) if cert_file and key_file else None,
    )


def _parse_rfc3339(text: str) -> float | None:
    """Vault timestamps are RFC 3339 with nanoseconds; an empty string means 'not set'."""
    if not text:
        return None
    trimmed = text.rstrip("Z")
    head, _, frac = trimmed.partition(".")
    try:
        moment = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return moment.timestamp() + (float("0." + frac) if frac else 0.0)
    except ValueError:
        raise SecretProviderUnavailable from None


class VaultKV2Provider:
    api_version: Literal["secret-provider/v1"] = API_VERSION
    scheme = "vault"

    def __init__(self, client: Any, *, clock: Any | None = None) -> None:
        self._client = client
        self._clock = clock or SystemClock()

    def cache_context(self) -> tuple[str, ...]:
        """The client's security context (address, namespace, token) for the cache key.

        The same KV path resolves to different secrets per namespace and per token's policies;
        the values only enter the salted cache-key HMAC and are never stored or rendered.
        """
        client = self._client
        namespace = getattr(getattr(client, "adapter", None), "namespace", None)
        return tuple(
            "" if value is None else str(value)
            for value in (getattr(client, "url", None), namespace, getattr(client, "token", None))
        )

    def describe(self, ref: SecretRef) -> SecretDescriptor:
        # No provider call: the pinned version is part of the reference.
        return SecretDescriptor(scheme=self.scheme, key_version=f"v{ref.version}", renewable=False)

    def resolve(self, ref: SecretRef) -> ResolvedSecret:
        mount, _, path = ref.locator.partition("/")
        failure: BaseException | None = None
        response: Any = None
        try:
            response = self._client.secrets.kv.v2.read_secret_version(
                path=path, version=int(ref.version or 0), mount_point=mount
            )
        except Exception as exc:
            failure = _map_sdk_error(exc)
        if failure is not None:
            raise failure
        return self._to_resolved(ref, response)

    def _to_resolved(self, ref: SecretRef, response: Any) -> ResolvedSecret:
        try:
            if (
                response.get("lease_id")
                or response.get("renewable")
                or response.get("lease_duration")
                or response.get("wrap_info")
                or response.get("auth")
            ):
                raise SecretLeaseUnsupported
            outer = response["data"]
            meta, data = outer["metadata"], outer["data"]
            if meta.get("destroyed"):
                raise SecretVersionUnavailable
            if int(meta["version"]) != int(ref.version or 0):
                raise SecretVersionUnavailable
            expires_at = _parse_rfc3339(str(meta.get("deletion_time") or ""))
            if expires_at is not None and expires_at <= self._clock.wall():
                raise SecretExpired
            created_at = _parse_rfc3339(str(meta.get("created_time") or ""))
            value = data[ref.field or ""]
            if not isinstance(value, str) or not value:
                raise SecretMaterialInvalid
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SecretProviderUnavailable from None
        return ResolvedSecret(
            SecretBuffer(value.encode("utf-8")),
            key_version=f"v{ref.version}",
            expires_at=expires_at,
            created_at=created_at,
        )

    def preflight(
        self, ref: SecretRef, requirements: tuple[Any, ...], cache_policy: SecretCachePolicy
    ) -> PreparedSecret:
        from decoy_engine.secret_providers._preflight import prepare_secret

        return prepare_secret(
            provider=self, ref=ref, requirements=requirements, cache_policy=cache_policy
        )


def _map_sdk_error(exc: BaseException) -> BaseException:
    """Map a vendor exception to a fixed safe type by CLASS only; its text is never read."""
    hvac = _hvac()
    errors = hvac.exceptions
    if isinstance(exc, (errors.Forbidden, errors.Unauthorized)):
        return SecretAccessDenied()
    if isinstance(exc, errors.InvalidPath):
        return SecretVersionUnavailable()
    return SecretProviderUnavailable()
