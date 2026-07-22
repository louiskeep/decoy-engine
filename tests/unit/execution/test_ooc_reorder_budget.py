"""OOC-B milestone 1, task 1: the reorder budget ledger.

`resolve_reorder_budgets` is the single source of every allocation the
external-reorder sorter and its driving DuckDB connection use: one process
memory ceiling, split into the DuckDB and sorter fractions, plus a disk
ledger that must cover the 2x merge-amplification cost of a k-way external
merge. Both budgets fail closed (missing ceiling, undersized ceiling,
insufficient disk) rather than let a caller proceed unbounded.
"""

from __future__ import annotations

import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._reorder_budget import (
    F_DUCKDB,
    F_SORT,
    MIN_RUN_BYTES,
    require_disk,
    resolve_reorder_budgets,
)


def test_none_memory_budget_fails_closed():
    with pytest.raises(ExecutionError) as exc_info:
        resolve_reorder_budgets(None, 10**12)
    assert exc_info.value.code == "out_of_core_reorder_unbudgeted"


def test_none_disk_budget_fails_closed():
    with pytest.raises(ExecutionError) as exc_info:
        resolve_reorder_budgets(10**10, None)
    assert exc_info.value.code == "out_of_core_reorder_unbudgeted"


def test_fraction_invariant_holds():
    ceiling = 10**10
    budgets = resolve_reorder_budgets(ceiling, 10**12)
    assert budgets.process_ceiling_bytes == ceiling
    assert budgets.duckdb_memory_limit_bytes == round(F_DUCKDB * ceiling)
    assert budgets.run_bytes_cap == round(F_SORT * ceiling)
    assert budgets.duckdb_memory_limit_bytes + budgets.run_bytes_cap <= 0.70 * ceiling


def test_undersized_ceiling_rejected():
    # 0.15 * 10_000_000 = 1_500_000 bytes, far under the 8 MiB MIN_RUN_BYTES floor.
    with pytest.raises(ExecutionError) as exc_info:
        resolve_reorder_budgets(10_000_000, 10**12)
    assert exc_info.value.code == "out_of_core_reorder_budget_too_small"


def test_minimum_viable_ceiling_accepted():
    # Smallest ceiling whose F_SORT share clears MIN_RUN_BYTES must be accepted.
    ceiling = int(MIN_RUN_BYTES / F_SORT) + 1
    budgets = resolve_reorder_budgets(ceiling, 10**12)
    assert budgets.run_bytes_cap >= MIN_RUN_BYTES


def test_disk_ledger_rejects_when_insufficient():
    staging = 1_000
    output = 1_000_000
    ceiling = 10**10

    # Just short of staging + 3*output.
    too_small_budgets = resolve_reorder_budgets(ceiling, staging + 3 * output - 1)
    with pytest.raises(ExecutionError) as exc_info:
        require_disk(
            too_small_budgets,
            mandatory_staging_bytes=staging,
            estimated_output_bytes=output,
        )
    assert exc_info.value.code == "out_of_core_reorder_budget_too_small"

    enough_budgets = resolve_reorder_budgets(ceiling, staging + 3 * output)
    assert (
        require_disk(
            enough_budgets,
            mandatory_staging_bytes=staging,
            estimated_output_bytes=output,
        )
        is None
    )


def test_disk_ledger_accounts_two_x_merge():
    # Covers staging + 2*output (no merge-amplification term) but not
    # staging + 3*output (with it): proves the ledger charges the extra
    # merge-amplification copy, not just the join output and one run set.
    staging = 1_000
    output = 1_000_000
    ceiling = 10**10
    covers_two_x_budgets = resolve_reorder_budgets(ceiling, staging + 2 * output)
    with pytest.raises(ExecutionError) as exc_info:
        require_disk(
            covers_two_x_budgets,
            mandatory_staging_bytes=staging,
            estimated_output_bytes=output,
        )
    assert exc_info.value.code == "out_of_core_reorder_budget_too_small"
