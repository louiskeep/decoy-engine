"""The reorder budget ledger for the OOC-B external-reorder architecture.

Ported alongside `BoundedExternalSorter` (P4-A.2) as a foundational dependency;
its production consumer -- the bounded-stream FK route that co-sizes DuckDB and
the sorter -- lands in P4-A.3, which does not exist on this branch yet. On
`feat/native-phase3` this module is exercised only by the sorter's subprocess
RSS proof (to size a realistic cap). The design it encodes:

`resolve_reorder_budgets` is the ONE place a process memory ceiling becomes
every allocation the bounded-stream FK route will use: the DuckDB `memory_limit`
for the join+buffer phase, and the sorter's `run_bytes_cap` (its resident
buffer AND its per-merge-head cap, see `_external_sort.py`). Both fractions
are derived from a single `process_ceiling_bytes` rather than sized
independently, so the two can never drift apart or double-count the same
memory.

Within one FK edge the phases run sequentially (join+buffer, then merge,
then drain; see the milestone plan's "Architecture" section), so the
join+buffer phase is the only one where DuckDB and the sort buffer are
co-resident. `F_DUCKDB + F_SORT` therefore bounds THAT phase's peak, not a
sum across the whole edge. The reserved `1 - F_DUCKDB - F_SORT` share (>=
0.30) covers what neither fraction accounts for: Arrow batches in flight,
the drain phase's own O(batch) residency (reader head, cursor concat,
payload batch, resolved output all fit comfortably in this reserve), the
Python interpreter and allocator slop, and DuckDB routinely overshooting its
own `memory_limit` under real workloads (DuckDB's "Memory Management" /
"Tuning Workloads" guidance documents this; `memory_limit` bounds the buffer
manager, not the process). The invariant is asserted at import so a future
edit to either constant that breaks the reserve fails fast, not silently at
a customer's job.

The disk ledger (`require_disk`) is the OOC-B milestone-1 fix for a gap two
Codex plan-review rounds found: an external k-way merge briefly needs BOTH
the old runs and the new merged run on disk at once, a real "2x" cost that a
naive `staging + output` estimate misses entirely.
"""

from __future__ import annotations

from dataclasses import dataclass

from decoy_engine.execution._errors import ExecutionError

# Share of process_ceiling_bytes handed to the DuckDB connection driving the
# join+buffer phase (`memory_limit`). Kept well under 1.0 alongside F_SORT so
# a >= 0.30 reserve remains for what neither budget accounts for (see module
# docstring).
F_DUCKDB = 0.55

# Share of process_ceiling_bytes handed to the sorter: its resident write()
# buffer cap AND (divided by 2 * merge_fan_in) its per-merge-head cap during
# finish() (see _external_sort.py).
F_SORT = 0.15

# Enforced at import: F_DUCKDB and F_SORT are the join+buffer phase's two
# co-resident shares, and must leave real room for the reserve documented
# above. A future edit that widens either fraction past this line fails at
# import time, not on a customer's job. Rounded to 9 decimal places before
# comparing: binary floats cannot represent 0.55 + 0.15 exactly (it lands a
# hair over 0.70), and that representation error is not a real budget
# violation.
assert round(F_DUCKDB + F_SORT, 9) <= 0.70, (  # noqa: S101 -- import-time invariant, not a runtime check
    f"F_DUCKDB ({F_DUCKDB}) + F_SORT ({F_SORT}) must leave >= 0.30 of "
    "process_ceiling_bytes as reserve for the drain phase, Arrow batches in "
    "flight, Python/allocator overhead, and DuckDB's own overshoot."
)

# Sorting is not feasible below this per-run buffer size: a run this small
# would flush constantly, turning "external sort" into "spill every row".
MIN_RUN_BYTES = 8 * 1024 * 1024  # 8 MiB


@dataclass(frozen=True)
class ReorderBudgets:
    """Every allocation the bounded-stream FK route needs, derived from one
    process memory ceiling. See the module docstring for the fraction and
    disk-ledger reasoning."""

    process_ceiling_bytes: int
    duckdb_memory_limit_bytes: int
    run_bytes_cap: int
    merge_fan_in: int
    remaining_disk_bytes: int


def resolve_reorder_budgets(
    process_ceiling_bytes: int | None,
    remaining_disk_bytes: int | None,
    *,
    merge_fan_in: int = 16,
) -> ReorderBudgets:
    """Resolve one process memory ceiling into every budget the
    bounded-stream FK route needs.

    Fails closed (`out_of_core_reorder_unbudgeted`) when either the memory
    ceiling or the disk ledger is absent: this is the "stream intent with a
    missing budget fails closed" rule from the route-seam design decision
    (a large job on the resident fallback path is exactly the OOM this
    architecture exists to remove, so silently proceeding unbounded is never
    an option here). Also fails closed
    (`out_of_core_reorder_budget_too_small`) when the ceiling is so small
    that the sorter's share cannot clear `MIN_RUN_BYTES`.
    """
    if process_ceiling_bytes is None or remaining_disk_bytes is None:
        raise ExecutionError(
            code="out_of_core_reorder_unbudgeted",
            message=(
                "the bounded-stream FK route requires both a process memory "
                "budget and a disk budget; got "
                f"process_ceiling_bytes={process_ceiling_bytes!r}, "
                f"remaining_disk_bytes={remaining_disk_bytes!r}. Set both "
                "budgets explicitly, or use the resident (_batch_join.py) "
                "path for jobs that fit in memory."
            ),
        )
    duckdb_memory_limit_bytes = round(F_DUCKDB * process_ceiling_bytes)
    run_bytes_cap = round(F_SORT * process_ceiling_bytes)
    if run_bytes_cap < MIN_RUN_BYTES:
        raise ExecutionError(
            code="out_of_core_reorder_budget_too_small",
            message=(
                f"process_ceiling_bytes={process_ceiling_bytes} yields a "
                f"{run_bytes_cap}-byte sorter run cap (F_SORT={F_SORT}), "
                f"under the {MIN_RUN_BYTES}-byte minimum viable run size; "
                "increase the process memory budget."
            ),
        )
    return ReorderBudgets(
        process_ceiling_bytes=process_ceiling_bytes,
        duckdb_memory_limit_bytes=duckdb_memory_limit_bytes,
        run_bytes_cap=run_bytes_cap,
        merge_fan_in=merge_fan_in,
        remaining_disk_bytes=remaining_disk_bytes,
    )


def require_disk(
    budgets: ReorderBudgets,
    *,
    mandatory_staging_bytes: int,
    estimated_output_bytes: int,
) -> None:
    """Fail closed (`out_of_core_reorder_budget_too_small`) unless
    `budgets.remaining_disk_bytes` can cover this edge's full disk ledger.

    Required disk = `mandatory_staging_bytes` (caller-measured: child keys +
    payload + parent-stage bytes) + `duckdb_temp` (<= `estimated_output_bytes`,
    the join can spill up to its own output) + `sorter_runs` (~=
    `estimated_output_bytes`, the sorted runs on disk before merge) +
    `merge_amplification` (= `estimated_output_bytes`, the extra copy while
    the old runs and the newly merged run coexist during a k-way merge).
    The last three terms are each bounded by `estimated_output_bytes`, so the
    total is `mandatory_staging_bytes + 3 * estimated_output_bytes`.
    """
    required_bytes = mandatory_staging_bytes + 3 * estimated_output_bytes
    if required_bytes > budgets.remaining_disk_bytes:
        raise ExecutionError(
            code="out_of_core_reorder_budget_too_small",
            message=(
                f"this edge needs {required_bytes} bytes of disk "
                f"(staging={mandatory_staging_bytes} + 3x estimated output "
                f"{estimated_output_bytes} for duckdb temp + sorter runs + "
                f"merge amplification) but only {budgets.remaining_disk_bytes} "
                "bytes remain; increase the disk budget or reduce this edge's "
                "output."
            ),
        )


__all__ = [
    "F_DUCKDB",
    "F_SORT",
    "MIN_RUN_BYTES",
    "ReorderBudgets",
    "require_disk",
    "resolve_reorder_budgets",
]
