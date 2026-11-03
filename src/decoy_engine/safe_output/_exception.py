"""The closed exception label: an exception's class as a fixed, low-cardinality identifier.

Pattern source: the OpenTelemetry ``error.type`` attribute (a low-cardinality class identifier,
never the message), lifted from the exact-type table that ``phase.error_result`` held.
A class name is chosen by whoever defined the class, so a caller's exception could carry data in
its name; the label is therefore picked by exact type from a closed set the engine trusts
(Python's builtin exceptions, the engine families the reason adapter registers, and the classes
of ``decoy_engine.errors``), and anything else, a subclass included, is ``EXCEPTION``.

The member set is exactly the names ``safe_error_class`` accepts, plus ``Exception``, so the two
cannot drift. Members are built at import from names only; the type table is built on first use
from the trusted classes, since resolving the families imports their modules.
"""

from __future__ import annotations

import builtins
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING

from decoy_engine import errors, reason_adapter

_EXCEPTION = "Exception"


def _is_exception_class(value: object) -> bool:
    return isinstance(value, type) and issubclass(value, BaseException)


def _member_names() -> frozenset[str]:
    names = {n for n, v in vars(builtins).items() if _is_exception_class(v)}
    names |= {n for n, v in vars(errors).items() if _is_exception_class(v)}
    names |= {name for _, name, _ in reason_adapter._CODED}
    names |= {name for _, name, _ in reason_adapter._FIXED}
    return frozenset(names | {_EXCEPTION})


_MEMBER_NAMES = _member_names()


class _SafeErrorTypeBase(str, Enum):
    @classmethod
    def from_label(cls, label: object) -> SafeErrorType:
        """The member whose value is exactly ``label``, else ``EXCEPTION`` (stored input)."""
        if type(label) is SafeErrorType:
            return label
        if type(label) is str and label in _MEMBER_NAMES:
            return SafeErrorType(label)
        return SafeErrorType.EXCEPTION


if TYPE_CHECKING:

    class SafeErrorType(_SafeErrorTypeBase):
        """A closed exception label (members are built at import)."""

        EXCEPTION = "Exception"

else:
    # The builtin exception names differ by interpreter, so the members are not written out.
    SafeErrorType = _SafeErrorTypeBase(
        "SafeErrorType",
        [("EXCEPTION" if n == _EXCEPTION else n, n) for n in sorted(_MEMBER_NAMES)],
        module=__name__,
        qualname="SafeErrorType",
    )
    SafeErrorType.__doc__ = "A closed exception label (members are built at import)."


@lru_cache(maxsize=1)
def _allowed_exception_types() -> dict[type, SafeErrorType]:
    """Exact class to member over the closed trusted set; a builtin maps by its own name."""
    classes = {v for v in vars(builtins).values() if _is_exception_class(v)}
    classes |= {v for v in vars(errors).values() if _is_exception_class(v)}
    classes |= reason_adapter.registered_families()
    return {cls: SafeErrorType(cls.__name__) for cls in classes if cls.__name__ in _MEMBER_NAMES}


def safe_error_type(exc: BaseException) -> SafeErrorType:
    """The label for ``exc`` by exact type; an unlisted class or a subclass is ``EXCEPTION``."""
    return _allowed_exception_types().get(type(exc), SafeErrorType.EXCEPTION)
