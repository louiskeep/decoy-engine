"""F31 E0a: the closed exception label is chosen by exact type, never by a class name."""

from __future__ import annotations

import pytest

from decoy_engine.safe_output import SafeErrorType, safe_error_type
from decoy_engine.subset._errors import SubsetError

_MARKER = "F31CANARY9d2c"


class _SubsetChildError(SubsetError):
    pass


# A caller-defined class that borrows a builtin's name.
_HostileOSError = type("OSError", (Exception,), {})


@pytest.mark.parametrize(
    "exc",
    [_SubsetChildError.__new__(_SubsetChildError), _HostileOSError()],
    ids=["subclass_of_listed", "hostile_named_class"],
)
def test_exact_type_only(exc: BaseException) -> None:
    assert safe_error_type(exc) is SafeErrorType.EXCEPTION


def test_listed_types_name_themselves() -> None:
    assert safe_error_type(KeyError(_MARKER)).value == "KeyError"
    assert safe_error_type(SubsetError.__new__(SubsetError)).value == "SubsetError"
    assert safe_error_type(Exception(_MARKER)) is SafeErrorType.EXCEPTION


@pytest.mark.parametrize("label", ["", _MARKER, 3, None], ids=["empty", "marker", "int", "none"])
def test_from_label_unknown_is_exception(label: object) -> None:
    assert SafeErrorType.from_label(label) is SafeErrorType.EXCEPTION


def test_from_label_known_value_and_str_subclass() -> None:
    class _Str(str):
        pass

    assert SafeErrorType.from_label("KeyError").value == "KeyError"
    assert SafeErrorType.from_label(_Str("KeyError")) is SafeErrorType.EXCEPTION
