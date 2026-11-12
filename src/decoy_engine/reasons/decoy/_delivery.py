"""Delivery decisions, failures and warnings of a multi-table run."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "DELIVERY_DEPENDENT_DROPPED",
    "DELIVERY_POINTER_MOVED",
    "DELIVERY_ROLLED_BACK",
    "DELIVERY_WITHHELD",
]

_d = _catalog.decoy("DecoyDelivery")

# Stub: wrong on purpose so the pinned tests fail by assertion before the real declarations.
DELIVERY_WITHHELD = _d("delivery_withheld", "A table that ran well was not delivered.")
DELIVERY_DEPENDENT_DROPPED = _d(
    "delivery_dependent_dropped", "A table that depends on a failed one was dropped."
)
DELIVERY_POINTER_MOVED = _d("delivery_pointer_moved", "Another run moved the current pointer.")
DELIVERY_ROLLED_BACK = _d("delivery_rolled_back", "A crashed batch was rolled back.")
