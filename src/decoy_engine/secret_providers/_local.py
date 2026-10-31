"""``env:`` and ``file:`` providers on the ``secret-provider/v1`` contract.

The ``file:`` read goes through the installed file-access policy (``FileRole.SECRET``) and is
refused unless the file is owner-only, checked with ``fstat`` on the descriptor the bytes are read
from (E1; OpenSSH's "UNPROTECTED PRIVATE KEY FILE" rule, without a path ``stat`` then reopen).
Under a strict policy the closed set of policy and resource failures leaves as a carrier the
admission boundary rebuilds (``_errors.KEPT_FILE_FAILURES``); everything else is the value-free
``MissingMaskSecret``.
"""

from __future__ import annotations

import errno
import os
import stat
from typing import Any, Literal

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileRole
from decoy_engine.keyprovider import MissingMaskSecret
from decoy_engine.secret_providers._buffer import SecretBuffer
from decoy_engine.secret_providers._errors import (
    MISSING_MESSAGE,
    SECRET_FIELD_KEY,
    KeptFileFailure,
    SecretFilePermissionsUnsafe,
    kept_kind,
)
from decoy_engine.secret_providers._types import (
    API_VERSION,
    PreparedSecret,
    ResolveContext,
    ResolvedSecret,
    SecretCachePolicy,
    SecretDescriptor,
    SecretRef,
)

_DEFAULT_KEY_VERSION = "v1"


class _LocalProvider:
    """Local providers are never cached (the resolver enforces it, see ``_preflight``)."""

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

    def resolve(self, ref: SecretRef, context: ResolveContext) -> ResolvedSecret:
        # No security selectors: the reference alone names the variable or file.
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


def _owner_only(info: os.stat_result) -> None:
    # A directory is a missing secret, decided before its (usually 0755) mode is judged.
    if stat.S_ISDIR(info.st_mode):
        raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR))
    # NTFS ACLs are not inspected; the mode bits there do not describe access.
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise SecretFilePermissionsUnsafe


class FileSecretProvider(_LocalProvider):
    scheme = "file"

    def _read(self, ref: SecretRef) -> bytes:
        kept: str | None = None
        try:
            return fileaccess.read_checked(
                FileRole.SECRET, ref.locator, SECRET_FIELD_KEY, _owner_only
            )
        except SecretFilePermissionsUnsafe:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            # Strict policies keep the closed set; the ambient CLI keeps main's behavior.
            kept = None if fileaccess.is_ambient() else kept_kind(exc)
            if kept is None and not isinstance(exc, OSError):
                raise
        # Raised outside the handler: neither carrier nor error has the read's exception as context.
        if kept is not None:
            raise KeptFileFailure(kept)
        raise MissingMaskSecret(MISSING_MESSAGE)
