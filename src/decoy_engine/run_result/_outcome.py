"""The one status and exit-code derivation for a run (D4 and D5 of the RESULT handoff).

Pure and I/O free. The exit integers are the caller's data (``exit_map``); the engine never
imports a CLI constant. A missing map key is a defect and raises.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import get_args

from decoy_engine.run_result._model import (
    DeliveryMode,
    DeliveryOutcome,
    FailureCategory,
    RunStatus,
)


@dataclass(frozen=True)
class Outcome:
    status: RunStatus
    exit_code: int


def derive_outcome(
    *,
    category: FailureCategory | None,
    delivery_mode: DeliveryMode,
    delivery_outcome: DeliveryOutcome,
    blocking_failed: bool,
    exit_map: Mapping[tuple[str, str | None], int],
) -> Outcome:
    """Status in order: cancelled, failed, partially delivered, findings, succeeded."""
    if category is not None and category not in get_args(FailureCategory):
        raise ValueError("unknown failure category")
    if delivery_mode not in get_args(DeliveryMode):
        raise ValueError("unknown delivery mode")
    if delivery_outcome not in get_args(DeliveryOutcome):
        raise ValueError("unknown delivery outcome")
    status: RunStatus
    if category == "cancelled":
        status = "cancelled"
    elif category is not None:
        status = "failed"
    elif delivery_mode == "best_effort" and delivery_outcome == "partial":
        status = "partially_delivered"
    elif blocking_failed:
        status = "findings"
    else:
        status = "succeeded"
    try:
        return Outcome(status, exit_map[(status, category)])
    except KeyError:
        raise ValueError("exit map has no entry for this status and category") from None
