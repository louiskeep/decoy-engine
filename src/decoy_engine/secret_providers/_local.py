"""``env:`` and ``file:`` providers on the ``secret-provider/v1`` contract."""

from __future__ import annotations

import os
from typing import Any, Literal

from decoy_engine.keyprovider import MissingMaskSecret
from decoy_engine.secret_providers._buffer import SecretBuffer
from decoy_engine.secret_providers._errors import MISSING_MESSAGE
from decoy_engine.secret_providers._types import (
    API_VERSION,
    PreparedSecret,
    ResolvedSecret,
    SecretCachePolicy,
    SecretDescriptor,
    SecretRef,
)

_DEFAULT_KEY_VERSION = "v1"


class _LocalProvider:
    api_version: Literal["secret-provider/v1"] = API_VERSION
    scheme = ""

    def describe(self, ref: SecretRef) -> SecretDescriptor:
        # Value-free: never touches the environment or the filesystem.
        return SecretDescriptor(
            scheme=self.scheme,
            key_version=ref.key_version or _DEFAULT_KEY_VERSION,
            cache_status="disabled",
        )

    def _read(self, ref: SecretRef) -> bytes:  # pragma: no cover - overridden
        raise NotImplementedError

    def resolve(self, ref: SecretRef) -> ResolvedSecret:
        return ResolvedSecret(
            SecretBuffer(self._read(ref)), key_version=ref.key_version or _DEFAULT_KEY_VERSION
        )

    def preflight(
        self, ref: SecretRef, requirements: tuple[Any, ...], cache_policy: SecretCachePolicy
    ) -> PreparedSecret:
        from decoy_engine.secret_providers._preflight import prepare_secret

        return prepare_secret(
            provider=self, ref=ref, requirements=requirements, cache_policy=cache_policy
        )


class EnvSecretProvider(_LocalProvider):
    scheme = "env"

    def _read(self, ref: SecretRef) -> bytes:
        value = os.environ.get(ref.locator)
        if value is None:
            raise MissingMaskSecret(MISSING_MESSAGE)
        return value.encode("utf-8")


class FileSecretProvider(_LocalProvider):
    scheme = "file"

    def _read(self, ref: SecretRef) -> bytes:
        try:
            with open(ref.locator, "rb") as handle:
                return handle.read()
        except OSError:
            raise MissingMaskSecret(MISSING_MESSAGE) from None
