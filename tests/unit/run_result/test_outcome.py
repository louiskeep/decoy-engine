"""AT-E4: ``derive_outcome`` is the one status and exit derivation."""

from __future__ import annotations

import itertools

import pytest

from decoy_engine.run_result import derive_outcome
from decoy_engine.run_result._outcome import Outcome

CATEGORIES = (None, "usage", "parameter", "runtime", "capacity", "cancelled")
MODES = ("in_place", "strict", "best_effort")
OUTCOMES = ("delivered", "partial", "nothing", "not_applicable")
STATUSES = ("succeeded", "findings", "partially_delivered", "failed", "cancelled")

# Distinct integers, so a status that reads the wrong key cannot match by luck.
EXIT_MAP = {
    ("succeeded", None): 10,
    ("findings", None): 14,
    ("partially_delivered", None): 16,
    ("failed", "usage"): 21,
    ("failed", "parameter"): 22,
    ("failed", "runtime"): 23,
    ("failed", "capacity"): 25,
    ("cancelled", "cancelled"): 31,
}


def oracle(category, mode, outcome, blocking):
    if category == "cancelled":
        return "cancelled"
    if category is not None:
        return "failed"
    if mode == "best_effort" and outcome == "partial":
        return "partially_delivered"
    return "findings" if blocking else "succeeded"


def call(category=None, mode="in_place", outcome="delivered", blocking=False, exit_map=EXIT_MAP):
    return derive_outcome(
        category=category,
        delivery_mode=mode,
        delivery_outcome=outcome,
        blocking_failed=blocking,
        exit_map=exit_map,
    )


def test_outcome_table() -> None:
    for category, mode, outcome, blocking in itertools.product(
        CATEGORIES, MODES, OUTCOMES, (False, True)
    ):
        got = call(category, mode, outcome, blocking)
        status = oracle(category, mode, outcome, blocking)
        key = (status, category if status in ("failed", "cancelled") else None)
        assert got == Outcome(status, EXIT_MAP[key]), (category, mode, outcome, blocking)


def test_named_cases() -> None:
    assert call(mode="best_effort", outcome="partial").status == "partially_delivered"
    assert call(mode="best_effort", outcome="partial").exit_code == 16
    # Only best_effort maps a partial delivery to partially_delivered; elsewhere it leaves the status.
    assert call(mode="in_place", outcome="partial").status == "succeeded"
    # A failure outranks a failed blocking check; a partial best-effort run outranks findings.
    assert call(category="runtime", blocking=True).status == "failed"
    assert (
        call(mode="best_effort", outcome="partial", blocking=True).status == "partially_delivered"
    )
    assert call(blocking=True) == Outcome("findings", 14)


@pytest.mark.parametrize("missing", sorted(EXIT_MAP, key=str))
def test_missing_map_key_is_a_defect(missing) -> None:
    short = {k: v for k, v in EXIT_MAP.items() if k != missing}
    status, category = missing
    with pytest.raises(ValueError):
        call(
            category=category,
            mode="best_effort" if status == "partially_delivered" else "in_place",
            outcome="partial" if status == "partially_delivered" else "delivered",
            blocking=status == "findings",
            exit_map=short,
        )


def test_unknown_inputs_are_refused() -> None:
    for kwargs in ({"category": "weird"}, {"mode": "weird"}, {"outcome": "weird"}):
        with pytest.raises(ValueError):
            call(**kwargs)
