"""Finite code families of the ``plan`` namespace: one explicit entry per member."""

from __future__ import annotations

from collections.abc import Mapping

from decoy_engine.reasons import plan
from decoy_engine.reasons._types import ReasonCode

__all__ = ["GROUPED_SERIES_MISSING_REF_BY_KEY"]

# The only two reference keys a grouped_series column carries.
GROUPED_SERIES_MISSING_REF_BY_KEY: Mapping[str, ReasonCode] = {
    "group_by": plan.GROUPED_SERIES_MISSING_GROUP_BY_REF,
    "order_by": plan.GROUPED_SERIES_MISSING_ORDER_BY_REF,
}
