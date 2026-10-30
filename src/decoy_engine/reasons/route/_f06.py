"""Catalog of the ``route`` namespace: F06 declared-casting route and sink admission."""

from __future__ import annotations

from typing import Any

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Kind, Outcome, Phase, RetryClass, Severity

__all__ = [
    "DECLARED_CASTING_FULL_FRAME",
    "DECLARED_CAST_INPUT_TYPE_INCOMPATIBLE",
    "DECLARED_CAST_ROUTE_UNSUPPORTED",
    "PROTECTED_QUARANTINE_UNAVAILABLE",
]

_d = _catalog.route("decoy_engine.declared_casting")
# A refusal before any source is read: the job or the environment must change, nothing is retried.
_REFUSAL: dict[str, Any] = {
    "kind": Kind.FAILURE,
    "severity": Severity.ERROR,
    "outcome": Outcome.CONFIGURATION,
    "retry": RetryClass.CONFIGURATION_REPAIR,
    "phases": (Phase.RESOLVE, Phase.COMPILE),
    "since": "1.1.0",
}

DECLARED_CASTING_FULL_FRAME = _d(
    "declared_casting_full_frame",
    "A job with declared casting contracts runs on the full-frame route.",
    since="1.1.0",
)
DECLARED_CAST_ROUTE_UNSUPPORTED = _d(
    "declared_cast_route_unsupported",
    "The selected route cannot apply a declared casting contract.",
    **_REFUSAL,
)
DECLARED_CAST_INPUT_TYPE_INCOMPATIBLE = _d(
    "declared_cast_input_type_incompatible",
    "An input column's physical type does not match its declared lexical contract.",
    **_REFUSAL,
)
PROTECTED_QUARANTINE_UNAVAILABLE = _d(
    "protected_quarantine_unavailable",
    "A protected quarantine store with the required protection is not available.",
    **{**_REFUSAL, "retry": RetryClass.ENVIRONMENT_REPAIR},
)
