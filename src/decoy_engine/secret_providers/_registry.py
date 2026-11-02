"""SecretProviderRegistry: scheme -> provider. Not ``providers_v2.ProviderRegistry``."""

from __future__ import annotations

import threading

from decoy_engine.secret_providers._errors import SecretProviderUnknown
from decoy_engine.secret_providers._types import API_VERSION, SecretProvider


class SecretProviderRegistry:
    def __init__(self, providers: tuple[SecretProvider, ...] = ()) -> None:
        self._lock = threading.Lock()
        self._providers: dict[str, SecretProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: SecretProvider) -> None:
        if getattr(provider, "api_version", None) != API_VERSION:
            raise ValueError(f"provider must implement {API_VERSION}")
        scheme = getattr(provider, "scheme", None)
        if not isinstance(scheme, str) or not scheme or scheme != scheme.lower():
            raise ValueError("provider scheme must be a non-empty lowercase string")
        with self._lock:
            if scheme in self._providers:
                raise ValueError(f"a provider is already registered for scheme {scheme!r}")
            self._providers[scheme] = provider

    def get(self, scheme: str) -> SecretProvider:
        with self._lock:
            provider = self._providers.get(scheme)
        if provider is None:
            raise SecretProviderUnknown
        return provider

    def schemes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._providers))


_DEFAULT: SecretProviderRegistry | None = None
_DEFAULT_LOCK = threading.Lock()


def default_registry() -> SecretProviderRegistry:
    """Process default registry: ``env`` and ``file``. Others are registered explicitly."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            from decoy_engine.secret_providers._local import EnvSecretProvider, FileSecretProvider

            _DEFAULT = SecretProviderRegistry((EnvSecretProvider(), FileSecretProvider()))
        return _DEFAULT


def register_secret_provider(provider: SecretProvider) -> None:
    default_registry().register(provider)
