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

import errno
import os
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
from decoy_engine.reasons import ReasonCode

BAD_REF_MESSAGE = "The secret reference is invalid."
BAD_ENCODING_MESSAGE = "The secret material has an invalid encoding."
# The config position a `file:` secret is read at; a strict file-access policy names it in text.
SECRET_FIELD_KEY = "global_settings.mask_secret_ref"  # noqa: S105 - a config position, not a credential
BAD_ENCODING_AT_POSITION_MESSAGE = (
    f"The secret material at {SECRET_FIELD_KEY} has an invalid encoding."
)
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


class SecretFilePermissionsUnsafe(SecretProviderError):  # noqa: N818 -- reason-named error; the name mirrors its registered code
    _MESSAGE = "The secret file is readable by other users; make it owner-only (mode 0600 or 0400)."

    def __init__(self) -> None:
        super().__init__(code=reasons.runtime.SECRET_FILE_PERMISSIONS_UNSAFE, message=self._MESSAGE)


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
        SecretFilePermissionsUnsafe,
    )
}
_CLOSED[reasons.runtime.KEYED_STRATEGY_REQUIRES_SECRET] = lambda: KeyedStrategyRequiresSecret(
    KEY_REQUIRED_GUIDANCE
)
_CLOSED[reasons.runtime.MISSING_MASK_SECRET] = lambda: MissingMaskSecret(MISSING_MESSAGE)
_CLOSED[reasons.runtime.WEAK_MASK_SECRET] = lambda: WeakMaskSecret(WEAK_MESSAGE)

# Engine-owned constant texts that keep their precision; any other text falls back to the first.
_BAD_REF_TEXTS = (
    BAD_REF_MESSAGE,
    BAD_ENCODING_MESSAGE,
    NEEDS_ENCODING_MESSAGE,
    BAD_ENCODING_AT_POSITION_MESSAGE,
)

Failure = tuple[str, str]
"""A registered code and an engine-owned text: the only things a failure keeps."""

_UNAVAILABLE: Failure = (reasons.runtime.SECRET_PROVIDER_UNAVAILABLE, "")
_REGISTERED = {code: code for code in _CLOSED}
_CODE_TYPES = (str, ReasonCode)

UNAVAILABLE_MESSAGE = "the installed file-access policy cannot serve the mask secret file"


def _kept_os_error(code: int) -> Callable[[], BaseException]:
    return lambda: OSError(code, os.strerror(code))


def _kept_denied() -> BaseException:
    from decoy_engine.fileaccess import FileAccessDenied

    return FileAccessDenied(SECRET_FIELD_KEY)


def _kept_unavailable() -> BaseException:
    from decoy_engine.fileaccess import FileAccessUnavailable

    return FileAccessUnavailable(UNAVAILABLE_MESSAGE)


# A-4 x F13 (owner decision 2): the CLOSED set of classes a strict file-access policy's `file:`
# secret read keeps across the admission boundary. Each entry rebuilds a fresh instance from
# constants only; nothing of the original exception (args, errno text, filename, chain) survives.
KEPT_FILE_FAILURES: dict[str, Callable[[], BaseException]] = {
    "file_access_denied": _kept_denied,
    "file_access_unavailable": _kept_unavailable,
    "memory": MemoryError,
    "enomem": _kept_os_error(errno.ENOMEM),
    "enospc": _kept_os_error(errno.ENOSPC),
    "edquot": _kept_os_error(errno.EDQUOT),
}
_KEPT_ERRNO = {errno.ENOMEM: "enomem", errno.ENOSPC: "enospc", errno.EDQUOT: "edquot"}
_KEPT = "<kept file failure>"  # not a registered code: only `kept_error` turns it into an error


class KeptFileFailure(DecoyError):  # noqa: N818 - a carrier, not an error a caller sees
    """Raised only by the built-in file provider: names one member of the closed set, nothing
    else (no args, no text). The admission boundary turns it back into that class."""

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind

    def __repr__(self) -> str:
        return f"KeptFileFailure({self.kind!r})"


def kept_kind(exc: BaseException) -> str | None:
    """The closed-set member `exc` is, or None. Called by the file provider on its own read's
    exception; exact types for the policy classes so a subclass is not admitted by accident."""
    from decoy_engine.fileaccess import FileAccessDenied, FileAccessUnavailable

    kind = type(exc)
    if kind is FileAccessDenied:
        return "file_access_denied"
    if kind is FileAccessUnavailable:
        return "file_access_unavailable"
    if isinstance(exc, MemoryError):
        return "memory"
    if isinstance(exc, OSError) and type(exc.errno) is int:
        return _KEPT_ERRNO.get(exc.errno)
    return None


def is_kept(failure: tuple[str, str]) -> bool:
    return failure[0] == _KEPT


def kept_error(failure: tuple[str, str], *, outermost: bool) -> BaseException:
    """A fresh instance for a kept failure. An inner boundary re-raises the carrier so the next
    boundary recognizes it; only the outermost one hands the caller the real class."""
    _, kind = failure
    if not outermost:
        return KeptFileFailure(kind)
    return KEPT_FILE_FAILURES[kind]()


def _classify(exc: BaseException) -> Failure:
    if type(exc) is KeptFileFailure:
        kind = exc.kind
        if type(kind) is str and kind in KEPT_FILE_FAILURES:
            return (_KEPT, kind)
        return _UNAVAILABLE
    code = getattr(exc, "code", None) if isinstance(exc, MaskSecretError) else None
    # Exact types: a foreign str subclass could run code on compare or hash.
    if not isinstance(code, str) or type(code) not in _CODE_TYPES:
        return _UNAVAILABLE
    if code == reasons.runtime.BAD_SECRET_REF:
        message = getattr(exc, "message", None)
        if type(message) is not str:
            return (reasons.runtime.BAD_SECRET_REF, BAD_REF_MESSAGE)
        text = next((t for t in _BAD_REF_TEXTS if t == message), BAD_REF_MESSAGE)
        return (reasons.runtime.BAD_SECRET_REF, text)
    return (_REGISTERED.get(code, _UNAVAILABLE[0]), "")


def failure_of(exc: BaseException) -> Failure:
    """The registered code (and engine text) for ``exc``, built from engine constants only.

    Total: a hostile ``code`` or ``message`` attribute that raises yields
    ``secret_provider_unavailable``. Nothing of ``exc`` (message, args, notes, type, chain,
    traceback) is retained, so the caller may drop ``exc`` and raise outside its handler.
    """
    try:
        return _classify(exc)
    except BaseException:
        return _UNAVAILABLE


def error_for(failure: Failure) -> MaskSecretError:
    """A freshly built engine error for a recorded ``failure``."""
    code, text = failure
    if code == reasons.runtime.BAD_SECRET_REF:
        return MaskSecretError(code=reasons.runtime.BAD_SECRET_REF, message=text or BAD_REF_MESSAGE)
    make = _CLOSED.get(code)
    return make() if make is not None else SecretProviderUnavailable()


def closed_error(exc: BaseException) -> MaskSecretError:
    """A freshly built, engine-owned error for ``exc``: same registered code, engine text only."""
    return error_for(failure_of(exc))
