"""SecretBuffer: the one owner of secret bytes between a provider and the key derivation.

Pattern: an owned mutable buffer with a constant redacted rendering, no serialization hooks and
an explicit close that overwrites it (the shape of ``bytearray``-backed secret holders such as
``cryptography``'s own key handling and Go's ``memguard`` enclave, without OS page locking).
Python, base64 decoders and cryptographic libraries make immutable copies, so clearing is best
effort, never a zeroization guarantee.
"""

from __future__ import annotations

from typing import NoReturn

from decoy_engine.secret_providers._errors import SecretClosedError

_REDACTED = "<redacted secret>"


class SecretBuffer:
    """Mutable, redacted, non-serializable byte storage with an explicit lifetime."""

    __slots__ = ("_buf", "_closed")

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        self._buf = bytearray(data)
        self._closed = False

    @classmethod
    def adopt(cls, owned: bytearray) -> SecretBuffer:
        """Take ownership of ``owned`` without copying; the caller must drop its reference."""
        self = cls.__new__(cls)
        self._buf = owned
        self._closed = False
        return self

    @property
    def closed(self) -> bool:
        return self._closed

    def view(self) -> memoryview:
        """A read-only view for validators and derivers. Do not retain it past the call."""
        if self._closed:
            raise SecretClosedError
        return memoryview(self._buf).toreadonly()

    def copy(self) -> SecretBuffer:
        return SecretBuffer(self.view())

    def close(self) -> None:
        if self._closed:
            return
        self._buf[:] = bytes(len(self._buf))
        self._closed = True

    def __enter__(self) -> SecretBuffer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return _REDACTED

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return _REDACTED

    def _refuse(self, *_: object, **__: object) -> NoReturn:
        raise TypeError("secret buffers cannot be copied, pickled or serialized")

    __reduce__ = _refuse
    __reduce_ex__ = _refuse
    __copy__ = _refuse
    __deepcopy__ = _refuse
    __getstate__ = _refuse
