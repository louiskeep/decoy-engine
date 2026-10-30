"""Closed error hierarchy of the secret-provider contract.

Every class carries a fixed registered F29 code and a constant message; nothing a provider,
SDK or reference says ever reaches ``str(exc)``. The admission choke point raises these
outside any ``except`` block (``__cause__`` and ``__context__`` stay ``None``), so
``logging.exception`` and excepthooks cannot print provider internals.

``closed_error`` is the trust boundary for errors: a caught exception is never forwarded, it is
rebuilt from its registered code alone (the closed-enumeration pattern of gRPC status codes and
RFC 9457 problem types: the code is the contract, free-form detail is dropped at the boundary).
"""

from __future__ import annotations

from collections.abc import Callable

from decoy_engine import reasons
from decoy_engine.errors import DecoyError
from decoy_engine.keyprovider import (
    KEY_REQUIRED_GUIDANCE,
    MIN_SECRET_BYTES,
    KeyedStrategyRequiresSecret,
    MaskSecretError,
    MissingMaskSecret,
    WeakMaskSecret,
)

BAD_REF_MESSAGE = "The secret reference is invalid."
BAD_ENCODING_MESSAGE = "The secret material has an invalid encoding."
NEEDS_ENCODING_MESSAGE = "This secret requirement needs an explicit encoding."
MISSING_MESSAGE = "The mask secret could not be resolved."
WEAK_MESSAGE = f"The mask secret must be at least {MIN_SECRET_BYTES} bytes."


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


_CLOSED: dict[str, Callable[[], MaskSecretError]] = {
    cls().code: cls
    for cls in (
        SecretProviderUnknown,
        SecretProviderUnavailable,
        SecretAccessDenied,
        SecretVersionUnavailable,
        SecretExpired,
        SecretLeaseUnsupported,
        SecretMaterialInvalid,
    )
}
_CLOSED[reasons.runtime.KEYED_STRATEGY_REQUIRES_SECRET] = lambda: KeyedStrategyRequiresSecret(
    KEY_REQUIRED_GUIDANCE
)
_CLOSED[reasons.runtime.MISSING_MASK_SECRET] = lambda: MissingMaskSecret(MISSING_MESSAGE)
_CLOSED[reasons.runtime.WEAK_MASK_SECRET] = lambda: WeakMaskSecret(WEAK_MESSAGE)

# Engine-owned constant texts that keep their precision; any other text falls back to the first.
_BAD_REF_TEXTS = (BAD_REF_MESSAGE, BAD_ENCODING_MESSAGE, NEEDS_ENCODING_MESSAGE)


def closed_error(exc: BaseException) -> MaskSecretError:
    """A freshly built, engine-owned error for ``exc``: same registered code, engine text only.

    Nothing of ``exc`` survives but its code: not its message, args, attributes, type, chain
    or traceback (whose frames may hold secret material in their locals).
    """
    code = getattr(exc, "code", None) if isinstance(exc, MaskSecretError) else None
    if not isinstance(code, str):
        return SecretProviderUnavailable()
    if code == reasons.runtime.BAD_SECRET_REF:
        message = getattr(exc, "message", None)
        text = message if isinstance(message, str) and message in _BAD_REF_TEXTS else None
        return MaskSecretError(code=reasons.runtime.BAD_SECRET_REF, message=text or BAD_REF_MESSAGE)
    make = _CLOSED.get(code)
    return make() if make is not None else SecretProviderUnavailable()
