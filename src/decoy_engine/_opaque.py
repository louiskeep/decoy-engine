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

from collections.abc import Callable
from typing import Any, TypeVar

_T = TypeVar("_T")


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


def validate_after_init(cls: type[_T]) -> type[_T]:
    """Run a dataclass's `_validate` after its generated `__init__` has returned.

    A `__post_init__` that raises does so from inside the generated `__init__`, whose frame holds
    every field as a parameter, so a secret field's bytes ride on the refusal's traceback. Here
    the arguments travel boxed through a frame that holds only the box, the generated `__init__`
    returns (its frame is gone), and only then does `_validate` run.
    """
    generated: Callable[..., None] = cls.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        box = OpaqueBox(args, kwargs)
        del args, kwargs
        generated(self, *box.open()[0], **box.open()[1])
        self._validate()

    init.__wrapped__ = generated  # type: ignore[attr-defined]
    init.__name__ = init.__qualname__ = "__init__"
    cls.__init__ = init  # type: ignore[method-assign]
    return cls


__all__ = ["OpaqueBox", "validate_after_init"]
