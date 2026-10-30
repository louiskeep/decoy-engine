"""Catalog of the ``runtime`` namespace: F06 value failures and protected quarantine outcomes."""

from __future__ import annotations

from typing import Any

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Phase, RetryClass

__all__ = [
    "DECLARED_COBOL_INVALID",
    "DECLARED_CURRENCY_INVALID",
    "DECLARED_DATE_INVALID",
    "DECLARED_DECIMAL_INVALID",
    "DECLARED_DECIMAL_OVERFLOW",
    "DECLARED_DECIMAL_PRECISION_LOSS",
    "DECLARED_INTEGER_INVALID",
    "DECLARED_INTEGER_OVERFLOW",
    "DECLARED_OUTPUT_WIDTH_OVERFLOW",
    "FIXED_WIDTH_RECORD_OVERLONG",
    "PROTECTED_QUARANTINE_ACCESS_DENIED",
    "PROTECTED_QUARANTINE_EXPIRED",
    "PROTECTED_QUARANTINE_SEAL_FAILED",
    "PROTECTED_QUARANTINE_WRITE_FAILED",
]

_d = _catalog.runtime("decoy_engine.declared_casting")
# A value failure is repaired in the data, never by retrying the operation.
_VALUE: dict[str, Any] = {
    "retry": RetryClass.NEVER,
    "phases": (Phase.MASK, Phase.FINALIZE, Phase.UNKNOWN),
    "since": "1.1.0",
}
_STORE: dict[str, Any] = {
    "retry": RetryClass.ENVIRONMENT_REPAIR,
    "phases": (Phase.FINALIZE, Phase.PUBLISH, Phase.UNKNOWN),
    "since": "1.1.0",
}
_ACCESS: dict[str, Any] = {
    "retry": RetryClass.NEVER,
    "phases": (Phase.FINALIZE, Phase.UNKNOWN),
    "since": "1.1.0",
}

DECLARED_INTEGER_INVALID = _d(
    "declared_integer_invalid", "A value does not match the declared integer grammar.", **_VALUE
)
DECLARED_INTEGER_OVERFLOW = _d(
    "declared_integer_overflow", "A value is outside the declared integer range.", **_VALUE
)
DECLARED_DECIMAL_INVALID = _d(
    "declared_decimal_invalid", "A value does not match the declared decimal grammar.", **_VALUE
)
DECLARED_DECIMAL_PRECISION_LOSS = _d(
    "declared_decimal_precision_loss",
    "A value would lose digits under the declared decimal scale.",
    **_VALUE,
)
DECLARED_DECIMAL_OVERFLOW = _d(
    "declared_decimal_overflow", "A value exceeds the declared decimal precision.", **_VALUE
)
DECLARED_DATE_INVALID = _d(
    "declared_date_invalid", "A value is not a valid date under the declared format.", **_VALUE
)
DECLARED_CURRENCY_INVALID = _d(
    "declared_currency_invalid", "A value does not match the declared currency format.", **_VALUE
)
DECLARED_COBOL_INVALID = _d(
    "declared_cobol_invalid", "A value does not match the declared COBOL field encoding.", **_VALUE
)
DECLARED_OUTPUT_WIDTH_OVERFLOW = _d(
    "declared_output_width_overflow",
    "A rendered value exceeds the declared output width.",
    **_VALUE,
)
FIXED_WIDTH_RECORD_OVERLONG = _d(
    "fixed_width_record_overlong",
    "A fixed-width record is longer than the layout's declared record width.",
    retry=RetryClass.NEVER,
    phases=(Phase.RESOLVE, Phase.UNKNOWN),
    since="1.1.0",
)
PROTECTED_QUARANTINE_WRITE_FAILED = _d(
    "protected_quarantine_write_failed",
    "A row could not be written to the protected quarantine store.",
    **_STORE,
)
PROTECTED_QUARANTINE_SEAL_FAILED = _d(
    "protected_quarantine_seal_failed",
    "The protected quarantine artifact could not be sealed.",
    **_STORE,
)
PROTECTED_QUARANTINE_ACCESS_DENIED = _d(
    "protected_quarantine_access_denied",
    "Access to the protected quarantine artifact was denied.",
    **_ACCESS,
)
PROTECTED_QUARANTINE_EXPIRED = _d(
    "protected_quarantine_expired",
    "The protected quarantine artifact is past its retention period.",
    **_ACCESS,
)
