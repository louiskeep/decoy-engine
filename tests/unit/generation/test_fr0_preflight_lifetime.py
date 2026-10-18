"""Phase F FR0: reserve-before-allocate holds for the batch layout, and a table's streams do not
outlive the reservations that pay for them.

Both are lifetime properties the reservation-event order alone cannot show, so they probe real
allocation (`tracemalloc`) and real liveness (`weakref`).
"""

from __future__ import annotations

import gc
import tracemalloc
import weakref
from typing import Any

import pytest

from decoy_engine.generation import _faker_pool
from decoy_engine.generation._budget_ledger import (
    GenerationBudgetExceeded,
    GenerationBudgetLedger,
)
from decoy_engine.generation._streamed import run_generation
from tests.parity import _f0_fixtures as fx
from tests.parity._fr0_support import prepared_config

MIB = 1024 * 1024


def test_preflight_rejects_a_huge_batch_count_without_materializing_the_layout() -> None:
    """10M rows at `batch_rows=1` is 10M partition entries. The rejection must come from
    arithmetic on (rows, batch_rows), not from building that list first (it was ~80 MB of
    Python allocation before `GenerationBudgetExceeded`, with the ledger still at zero)."""
    config = prepared_config(
        fx.config([fx.table("t", 10_000_000, [{"name": "id", "type": "sequence"}])])
    )
    ledger = GenerationBudgetLedger(64 * MIB)
    tracemalloc.start()
    try:
        with pytest.raises(GenerationBudgetExceeded):
            run_generation(config, ledger=ledger, batch_rows=1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * MIB, f"preflight allocated {peak} bytes before rejecting"
    assert ledger.peak_bytes == 0


def test_a_supplied_layout_is_charged_before_any_stage_reserves() -> None:
    config = prepared_config(fx.config([fx.table("t", 50, [{"name": "id", "type": "sequence"}])]))
    ledger = GenerationBudgetLedger(None)
    run_generation(config, ledger=ledger, partition=lambda _t, rows: [1] * rows)
    stages = [stage for stage, _ in ledger.events]
    assert stages[0] == "partition_layout"
    assert ledger.events[0][1] > 0


def test_a_supplied_layout_that_does_not_fit_is_rejected_before_any_stage_reserves() -> None:
    """Same table, same ceiling: it fits with the arithmetic layout and is refused only because
    the supplied 500k-entry layout's bookkeeping is charged, and nothing else reserved first."""
    config = prepared_config(
        fx.config([fx.table("t", 50_000, [{"name": "id", "type": "sequence"}])])
    )
    sized = GenerationBudgetLedger(1 << 40, charged_bytes=0)
    run_generation(config, ledger=sized)
    ceiling = sized.peak_bytes * 3 // 2
    run_generation(config, ledger=GenerationBudgetLedger(ceiling))

    ledger = GenerationBudgetLedger(ceiling)
    with pytest.raises(GenerationBudgetExceeded):
        run_generation(
            config,
            ledger=ledger,
            partition=lambda _t, rows: [rows, *[0] * 500_000],
        )
    assert ledger.events == []


def test_a_tables_pool_is_freed_before_the_next_table_builds_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Table N's reservations are released when its stage closes, so its streams must be dead by
    then. Otherwise table N+1's pool is built while N's is still resident and uncharged."""
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", 40)
    refs: list[weakref.ReferenceType[Any]] = []
    first_alive_at_second_build: list[bool] = []
    real = _faker_pool.open_pooled_selector

    def probing(**kwargs: Any) -> Any:
        if refs:
            gc.collect()  # a real leak survives this; only a refcount-timing artifact would not
            first_alive_at_second_build.append(refs[0]() is not None)
        selector = real(**kwargs)
        refs.append(weakref.ref(selector))
        return selector

    monkeypatch.setattr(_faker_pool, "open_pooled_selector", probing)
    city = {"name": "city", "type": "faker", "faker_type": "city"}
    config = prepared_config(fx.config([fx.table("a", 50, [city]), fx.table("b", 50, [city])]))
    ledger = GenerationBudgetLedger(None)
    run_generation(config, ledger=ledger, batch_rows=10)
    assert len(refs) == 2
    assert first_alive_at_second_build == [False]
