"""``env:`` and ``file:`` providers on the ``secret-provider/v1`` contract.

A ``file:`` secret must be owner-only, the OpenSSH rule for private keys ("UNPROTECTED PRIVATE KEY
FILE": any group or other permission bit refuses the key). The mode is read with ``fstat`` on the
descriptor the bytes come from, never a path ``stat`` (CWE-367 check-then-use), so a path that is
swapped or re-pointed after the open cannot change the verdict. Symlinks are followed on purpose:
the Kubernetes secret volume is a symlink chain and the descriptor reports the final file.
"""

from __future__ import annotations

import contextlib
import os
import stat
from typing import Any, Literal

from decoy_engine.keyprovider import MissingMaskSecret
from decoy_engine.secret_providers._buffer import SecretBuffer
from decoy_engine.secret_providers._errors import MISSING_MESSAGE, SecretFilePermissionsUnsafe
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
_GROUP_OR_OTHER = stat.S_IRWXG | stat.S_IRWXO
# Without O_BINARY a Windows descriptor is opened in text mode and the CRT rewrites line endings.
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0)


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


class FileSecretProvider(_LocalProvider):
    scheme = "file"

    def _read(self, ref: SecretRef) -> bytes:
        try:
            fd = os.open(ref.locator, _OPEN_FLAGS)
        except OSError:
            fd = -1
        if fd < 0:
            raise MissingMaskSecret(MISSING_MESSAGE)
        data: bytes | None = None
        unsafe = False
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                pass  # the unreadable-file case, as before: a directory is not a secret file
            elif os.name != "nt" and st.st_mode & _GROUP_OR_OTHER:
                unsafe = True  # NTFS ACLs are not mode bits; Windows is not checked
            else:
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    data = handle.read()
        except OSError:
            data = None
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
        # Raised here, outside any handler, so no OS error text or path rides along as context.
        if unsafe:
            raise SecretFilePermissionsUnsafe()
        if data is None:
            raise MissingMaskSecret(MISSING_MESSAGE)
        return data
