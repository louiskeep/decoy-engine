"""Closed error hierarchy of the secret-provider contract.

Every class carries a fixed registered F29 code and a constant message; nothing a provider,
SDK or reference says ever reaches ``str(exc)``. The admission choke point raises these
outside any ``except`` block (``__cause__`` and ``__context__`` stay ``None``), so
``logging.exception`` and excepthooks cannot print provider internals.
"""

from __future__ import annotations

from decoy_engine import reasons
from decoy_engine.errors import DecoyError
from decoy_engine.keyprovider import MaskSecretError


class SecretProviderError(MaskSecretError):
    """Base of the provider failures. Subclasses fix the code; the message is constant."""

    _MESSAGE = "The secret provider failed."

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class SecretProviderUnknown(SecretProviderError):  # noqa: N818 -- reason-named error; the name mirrors its registered code
    _MESSAGE = "The referenced secret provider is not registered."

    def __init__(self) -> None:
        super().__init__(code=reasons.runtime.SECRET_PROVIDER_UNKNOWN, message=self._MESSAGE)


class SecretProviderUnavailable(SecretProviderError):  # noqa: N818 -- reason-named error; the name mirrors its registered code
    _MESSAGE = "The secret provider could not complete a safe read."

    def __init__(self) -> None:
        super().__init__(code=reasons.runtime.SECRET_PROVIDER_UNAVAILABLE, message=self._MESSAGE)


class SecretAccessDenied(SecretProviderError):  # noqa: N818 -- reason-named error; the name mirrors its registered code
    _MESSAGE = "The workload is not permitted to read the pinned secret."

    def __init__(self) -> None:
        super().__init__(code=reasons.runtime.SECRET_ACCESS_DENIED, message=self._MESSAGE)


class SecretVersionUnavailable(SecretProviderError):  # noqa: N818 -- reason-named error; the name mirrors its registered code
    _MESSAGE = "The pinned secret version is unavailable."

    def __init__(self) -> None:
        super().__init__(code=reasons.runtime.SECRET_VERSION_UNAVAILABLE, message=self._MESSAGE)


class SecretExpired(SecretProviderError):  # noqa: N818 -- reason-named error; the name mirrors its registered code
    _MESSAGE = "The selected secret is outside its usable lifetime."

    def __init__(self) -> None:
        super().__init__(code=reasons.runtime.SECRET_EXPIRED, message=self._MESSAGE)


class SecretLeaseUnsupported(SecretProviderError):  # noqa: N818 -- reason-named error; the name mirrors its registered code
    _MESSAGE = "The selected secret requires a dynamic lease that is not supported."

    def __init__(self) -> None:
        super().__init__(code=reasons.runtime.SECRET_LEASE_UNSUPPORTED, message=self._MESSAGE)


class SecretMaterialInvalid(SecretProviderError):  # noqa: N818 -- reason-named error; the name mirrors its registered code
    _MESSAGE = "The secret material does not meet the selected algorithm contract."

    def __init__(self) -> None:
        super().__init__(code=reasons.runtime.SECRET_MATERIAL_INVALID, message=self._MESSAGE)


class SecretClosedError(DecoyError):
    """A closed secret buffer or prepared secret was used (an internal invariant breach)."""

    def __init__(self) -> None:
        super().__init__("secret buffer is closed")
