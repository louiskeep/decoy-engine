"""Delivery reasons of a multi-table run: which tables went out, and how a swap or batch ended."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import UNRELEASED, Kind, Outcome, RetryClass, Severity

__all__ = [
    "DELIVERY_DEPENDENT_DROPPED",
    "DELIVERY_POINTER_MOVED",
    "DELIVERY_ROLLED_BACK",
    "DELIVERY_WITHHELD",
]

_d = _catalog.decoy("DecoyDelivery")

# The two decisions ride on a table's delivery state; the failed table keeps its own cause code.
DELIVERY_WITHHELD = _d(
    "delivery_withheld",
    "A table that ran well was not delivered because another table failed.",
    kind=Kind.DECISION,
    severity=Severity.INFO,
    outcome=Outcome.PUBLICATION,
    retry=RetryClass.NEVER,
    since=UNRELEASED,
)
DELIVERY_DEPENDENT_DROPPED = _d(
    "delivery_dependent_dropped",
    "A table that depends on a failed table was not delivered.",
    kind=Kind.DECISION,
    severity=Severity.INFO,
    outcome=Outcome.PUBLICATION,
    retry=RetryClass.NEVER,
    since=UNRELEASED,
)
# Only a lost compare-and-swap: a failed fsync after the swap is uncertain, not this.
DELIVERY_POINTER_MOVED = _d(
    "delivery_pointer_moved",
    "Another run moved the current pointer before this run could.",
    outcome=Outcome.PUBLICATION,
    retry=RetryClass.RETRY_OPERATION,
    since=UNRELEASED,
)
DELIVERY_ROLLED_BACK = _d(
    "delivery_rolled_back",
    "A crashed delivery batch was rolled back from its journal.",
    kind=Kind.WARNING,
    severity=Severity.WARNING,
    outcome=Outcome.PUBLICATION,
    retry=RetryClass.NEVER,
    since=UNRELEASED,
)
