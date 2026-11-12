"""F06 exception classes publish under their own names, never the generic "Exception"."""

from __future__ import annotations

import pytest

from decoy_engine import phase, reasons
from decoy_engine.contracts.declared_casting import DeclaredCastContractError
from decoy_engine.errors import FixedWidthRecordOverlongError
from decoy_engine.execution._errors import (
    DeclaredCastRouteError,
    DeclaredCastValueError,
    QuarantineError,
)
from decoy_engine.safe_output import SafeErrorType, safe_error_type

_F06_CLASSES = [
    DeclaredCastContractError,
    DeclaredCastRouteError,
    DeclaredCastValueError,
    QuarantineError,
    FixedWidthRecordOverlongError,
]


@pytest.mark.parametrize("cls", _F06_CLASSES, ids=lambda c: c.__name__)
def test_safe_error_type_names_f06_class(cls: type[BaseException]) -> None:
    label = safe_error_type(cls.__new__(cls))
    assert label is not SafeErrorType.EXCEPTION
    assert label.value == cls.__name__


@pytest.mark.parametrize("cls", _F06_CLASSES, ids=lambda c: c.__name__)
def test_error_result_reports_f06_class(cls: type[BaseException]) -> None:
    res = phase.error_result(
        "post_scan",
        "integrity",
        "m",
        "1",
        reasons.get_reason("post_scan", "check_error").ref,
        cls.__new__(cls),
    )
    wire = phase.canonical_bytes(res).decode()
    assert f'"error_type":"{cls.__name__}"' in wire
