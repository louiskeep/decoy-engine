"""JC-5 seam unit tests for `execution/native/_requirements.py` (Phase 3 Task
3.1): `NATIVE_POOL_STRATEGIES`, `faker_pool_precondition_met`, and
`native_pool_rejection`.

`tests/native/test_dispatch_faker.py` already covers this seam through the
full compiled-plan path (mostly `@_NEEDS_COMPANION`, since it exercises the
real dispatch route). These three functions are pure config predicates over a
`ColumnSeed` (no compiled kernel, no I/O), so this module unit-tests every
branch directly against a hand-built `ColumnSeed`, without the companion or a
full plan compile -- the same faker-only-lane pattern
`test_phase3_eligibility.py` uses for the layer above this one.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine.execution.native._requirements import (
    NATIVE_KERNEL_STRATEGIES,
    NATIVE_POOL_STRATEGIES,
    faker_pool_precondition_met,
    native_pool_rejection,
)
from decoy_engine.plan._types import ColumnSeed


class _Node:
    def __init__(self, plan_slice: Any) -> None:
        self.plan_slice = plan_slice


def _seed(
    *,
    deterministic: bool = True,
    cardinality_mode: str = "reuse",
    namespace: str | None = "ns",
    pool_size: int | None = 100,
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="faker",
        provider="person_first_name",
        backend_type="faker",
        backend_version="",
        cardinality_mode=cardinality_mode,  # type: ignore[arg-type]
        deterministic=deterministic,
        pool_size=pool_size,
    )


# ---------------------------------------------------------------------------
# NATIVE_POOL_STRATEGIES: a separate set from NATIVE_KERNEL_STRATEGIES.
# ---------------------------------------------------------------------------


def test_pool_strategies_set_is_disjoint_from_kernel_strategies() -> None:
    assert {"faker"} == NATIVE_POOL_STRATEGIES
    assert NATIVE_POOL_STRATEGIES.isdisjoint(NATIVE_KERNEL_STRATEGIES)


# ---------------------------------------------------------------------------
# faker_pool_precondition_met: one test per axis, each violating exactly one
# check against an otherwise-satisfying ColumnSeed.
# ---------------------------------------------------------------------------


def test_precondition_met_when_every_axis_satisfied() -> None:
    assert faker_pool_precondition_met(_Node(_seed())) is True


def test_precondition_rejects_non_column_seed_slice() -> None:
    assert faker_pool_precondition_met(_Node(object())) is False


def test_precondition_rejects_non_deterministic() -> None:
    assert faker_pool_precondition_met(_Node(_seed(deterministic=False))) is False


@pytest.mark.parametrize(
    "cardinality_mode",
    ["unique", "match_source_cardinality", "scale_source_cardinality"],
)
def test_precondition_rejects_non_reuse_cardinality_mode(cardinality_mode: str) -> None:
    assert faker_pool_precondition_met(_Node(_seed(cardinality_mode=cardinality_mode))) is False


@pytest.mark.parametrize("namespace", [None, ""])
def test_precondition_rejects_missing_namespace(namespace: str | None) -> None:
    assert faker_pool_precondition_met(_Node(_seed(namespace=namespace))) is False


def test_precondition_rejects_missing_pool_size() -> None:
    assert faker_pool_precondition_met(_Node(_seed(pool_size=None))) is False


# ---------------------------------------------------------------------------
# native_pool_rejection: strategy-set gate first, then the precondition,
# admit only when both pass.
# ---------------------------------------------------------------------------


def test_pool_rejection_admits_a_satisfying_faker_column() -> None:
    assert native_pool_rejection(_Node(_seed()), "FIRST", "faker") is None


def test_pool_rejection_rejects_a_strategy_outside_the_pool_set() -> None:
    # "hash" is a real strategy, just not a pool strategy -- goes through
    # `native_kernel_rejection` instead in `requirements_for`, never this
    # function in production, but the function itself must still be total.
    assert native_pool_rejection(_Node(_seed()), "H", "hash") == "no_native_pool_path:H:hash"


def test_pool_rejection_rejects_an_unmet_precondition_with_its_own_code() -> None:
    node = _Node(_seed(deterministic=False))
    assert (
        native_pool_rejection(node, "FIRST", "faker")
        == "faker_not_deterministic_reuse_variant:FIRST"
    )
