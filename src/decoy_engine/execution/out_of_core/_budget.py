"""Host-aware memory budget and spill-disk guard for the out-of-core route.

One knob bounds the route's memory: `resolve_budget` turns an explicit byte
budget (or, absent one, a conservative fraction of detected host RAM) into the
DuckDB `memory_limit` string `connect_duckdb` already accepts and a
`batch_rows` for the streaming passes. The sizing follows established
practice: DuckDB itself bounds its buffer manager with `memory_limit` and
spills operator state to `temp_directory` when the limit is hit (DuckDB
"Memory Management" / "Tuning Workloads" docs; its own default is 80% of RAM),
and the conservative-fraction default mirrors the heap-fraction conventions
long used by managed runtimes and databases (the JVM's default max heap of 1/4
of physical RAM via MaxRAMPercentage=25; PostgreSQL's canonical ~25%-of-RAM
shared_buffers guidance). A fraction, not the whole host, because the route's
Python/Arrow batch buffers and the OS page cache live outside DuckDB's
accounting.

`batch_rows` scales with the budget but is floored (a degenerate batch size
would grind the DuckDB round trips to a halt) and capped at the route's
pinned default (`_join._JOIN_BATCH_ROWS`), so a large host never silently
changes behavior versus the constant the parity suite pins, and no batch is
ever sized by table cardinality.

Spill is bounded too: `check_temp_disk_budget` measures the on-disk footprint
under the route's `temp_dir` (relation/join staging plus DuckDB spill) and
fails closed with a coded error before the disk fills, the same guard-rail
role as DuckDB's `max_temp_directory_size` setting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._join import _JOIN_BATCH_ROWS

# Conservative auto-detect fraction of host RAM (JVM default heap fraction).
_HOST_RAM_FRACTION = 0.25

# Never budget below this even on a tiny or misdetected host: DuckDB plus the
# route's per-edge Arrow buffers need real working room to make progress.
_MIN_BUDGET_BYTES = 64 * 1024 * 1024

# One streamed batch should stay a small slice of the budget: several bounded
# copies of a batch are live at once per edge (raw batch, masked batch, join
# staging, sink write), so the divisor keeps their sum well inside the budget
# at the ~1 KiB/row working size of the measured wide-string chains.
_BATCH_BYTES_PER_ROW = 32 * 1024
_MIN_BATCH_ROWS = 1_024
_MAX_BATCH_ROWS = _JOIN_BATCH_ROWS

_PROC_MEMINFO = Path("/proc/meminfo")


@dataclass(frozen=True)
class OutOfCoreBudget:
    """A resolved memory budget for one out-of-core run."""

    budget_bytes: int
    memory_limit: str
    batch_rows: int


def detect_host_memory_bytes() -> int:
    """Total physical host RAM in bytes, without a psutil dependency.

    POSIX sysconf first (page size times physical pages), /proc/meminfo as the
    fallback; fails closed with a coded error where neither is available, so a
    caller never proceeds on a silently-invented budget.
    """
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
        if page_size > 0 and phys_pages > 0:
            return page_size * phys_pages
    except (AttributeError, OSError, ValueError):
        pass
    try:
        for line in _PROC_MEMINFO.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    raise ExecutionError(
        code="out_of_core_memory_detection_failed",
        message=(
            "host RAM could not be detected (no sysconf, no /proc/meminfo); "
            "pass an explicit budget_bytes instead."
        ),
    )


def resolve_budget(budget_bytes: int | None = None) -> OutOfCoreBudget:
    """Resolve one memory budget into DuckDB and batch-sizing knobs.

    With no explicit budget, a conservative fraction of detected host RAM is
    used. The result is floored so a tiny or misdetected budget never yields a
    zero/absurd limit, and `batch_rows` is clamped to the route's pinned
    default at the top end.
    """
    if budget_bytes is not None and budget_bytes <= 0:
        raise ExecutionError(
            code="out_of_core_budget_invalid",
            message=f"budget_bytes must be positive, got {budget_bytes}.",
        )
    if budget_bytes is None:
        budget_bytes = int(detect_host_memory_bytes() * _HOST_RAM_FRACTION)
    budget_bytes = max(budget_bytes, _MIN_BUDGET_BYTES)
    batch_rows = min(max(budget_bytes // _BATCH_BYTES_PER_ROW, _MIN_BATCH_ROWS), _MAX_BATCH_ROWS)
    # MiB count with a decimal "MB" suffix: DuckDB reads "MB" as base-10, so the
    # effective limit lands slightly BELOW budget_bytes. That is the safe
    # direction for a cap (never over the budget), and it keeps the string a
    # round MiB figure.
    return OutOfCoreBudget(
        budget_bytes=budget_bytes,
        memory_limit=f"{budget_bytes // (1024 * 1024)}MB",
        batch_rows=batch_rows,
    )


def temp_disk_bytes(temp_dir: Path) -> int:
    """Current on-disk footprint under `temp_dir`, in bytes.

    Files evicted between listing and stat are skipped: an eviction shrinks
    the footprint, so skipping it never hides an over-budget state.
    """
    total = 0
    if temp_dir.exists():
        for dirpath, _dirnames, filenames in os.walk(temp_dir):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
    return total


def check_temp_disk_budget(temp_dir: Path, *, max_bytes: int) -> int:
    """Fail closed once the spill footprint under `temp_dir` exceeds a budget.

    Returns the current footprint when under budget so a caller can log or
    poll it. Point-in-time by design: a runner calls it at its natural batch
    or table boundaries (no watcher thread), so a spike strictly between two
    checks is caught at the next boundary rather than the exact byte.
    """
    used = temp_disk_bytes(temp_dir)
    if used > max_bytes:
        raise ExecutionError(
            code="out_of_core_temp_disk_exceeded",
            message=(
                f"out-of-core temp disk footprint {used} bytes exceeds the "
                f"{max_bytes}-byte budget under {temp_dir}."
            ),
        )
    return used


__all__ = [
    "OutOfCoreBudget",
    "check_temp_disk_budget",
    "detect_host_memory_bytes",
    "resolve_budget",
    "temp_disk_bytes",
]
