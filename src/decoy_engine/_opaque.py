"""A holder for secret material in frames a traceback may keep.

`traceback.TracebackException(capture_locals=True)` (and debuggers, error trackers such as
Sentry's `include_local_variables`) render every local of every frame on the chain with `repr`.
A frame that holds key bytes in a local or parameter shows them. Engine entry points that take
or derive secret material move it into an `OpaqueBox` (fixed repr) and delete the plain name
before anything can raise, unpacking it only inside the call expression that consumes it, so
the bytes live on the value stack of that one call and never in a frame's locals. This is the
boxed-entry pattern of the secret-admission boundary (`secret_providers._preflight`).
"""

from __future__ import annotations

from typing import Any


class OpaqueBox:
    """Carries value-bearing objects through frames that traceback rendering may capture."""

    __slots__ = ("_items",)

    def __init__(self, *items: object) -> None:
        self._items = items

    def open(self) -> tuple[Any, ...]:
        return self._items

    def __repr__(self) -> str:
        return "<opaque>"

    __str__ = __repr__

    def __reduce_ex__(self, *_: object) -> Any:
        raise TypeError("opaque boxes cannot be pickled")


__all__ = ["OpaqueBox"]
