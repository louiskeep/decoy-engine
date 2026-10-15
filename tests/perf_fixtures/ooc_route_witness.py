"""Route/spill witness for the out-of-core FK route (D4).

Plan: docs/plans/2026-10-15-d4-scale-proof.md sections 2 and 3.1. Shared by the D4a
reachability test and `scripts/fk-scale/bench_worker_fk.py`, so the two read the
same evidence.

Why a witness and not "the class was constructed": `BoundedExternalSorter(...)`
being built proves only that the reorder lane was admitted. Evidence the lane did
its job is that runs were actually written to disk (`_write_run`), at least one k-way
merge ran (`_merge_group`), and the sorter's own resident peak stayed under its cap.
Spies wrap the real functions (same technique as
`tests/unit/execution/test_r3_ooc_reorder_reachability.py`), so every run still
executes and its output is unchanged.

Log hygiene: only counts, byte sizes and table names are recorded, never a value.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

import decoy_engine.execution.out_of_core as ooc_pkg
from decoy_engine.execution.out_of_core import _runner
from decoy_engine.execution.out_of_core._external_sort import (
    SORT_OVERHEAD_FACTOR,
    BoundedExternalSorter,
)


@dataclass
class SorterRecord:
    run_bytes_cap: int
    merge_fan_in: int
    runs_written: int = 0
    spilled_bytes: int = 0
    merge_groups: int = 0
    peak_buffered_bytes: int = 0
    peak_merge_resident_bytes: int = 0

    @property
    def peak_resident_bytes(self) -> int:
        return max(self.peak_buffered_bytes, self.peak_merge_resident_bytes)

    @property
    def resident_envelope_bytes(self) -> int:
        """The documented bound: `run_bytes_cap` times the flush-sort transient factor."""
        return int(self.run_bytes_cap * SORT_OVERHEAD_FACTOR)

    @property
    def spilled_and_merged(self) -> bool:
        return self.runs_written >= 2 and self.merge_groups >= 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_bytes_cap": self.run_bytes_cap,
            "merge_fan_in": self.merge_fan_in,
            "runs_written": self.runs_written,
            "spilled_bytes": self.spilled_bytes,
            "merge_groups": self.merge_groups,
            "peak_buffered_bytes": self.peak_buffered_bytes,
            "peak_merge_resident_bytes": self.peak_merge_resident_bytes,
            "resident_envelope_bytes": self.resident_envelope_bytes,
        }


@dataclass
class RouteWitness:
    """Which driver ran per table, plus what every sorter did."""

    driver_by_table: dict[str, str] = field(default_factory=dict)
    sorters: list[SorterRecord] = field(default_factory=list)
    # The sizing the pipeline route actually handed the runner (resolved budget, batch
    # size, disk budget), so a record states what ran, not what was requested.
    runner_args: dict[str, Any] = field(default_factory=dict)

    @property
    def reorder_tables(self) -> list[str]:
        return [t for t, d in self.driver_by_table.items() if d == "reorder"]

    @property
    def batch_tables(self) -> list[str]:
        return [t for t, d in self.driver_by_table.items() if d == "batch"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "driver_by_table": dict(self.driver_by_table),
            "runner_args": dict(self.runner_args),
            "reorder_taken": bool(self.reorder_tables),
            "spill": {
                "sorters": len(self.sorters),
                "runs_written": sum(s.runs_written for s in self.sorters),
                "spilled_bytes": sum(s.spilled_bytes for s in self.sorters),
                "merge_groups": sum(s.merge_groups for s in self.sorters),
                "spilled_and_merged": any(s.spilled_and_merged for s in self.sorters),
                "detail": [s.as_dict() for s in self.sorters],
            },
        }


@contextmanager
def witness_ooc_route() -> Iterator[RouteWitness]:
    """Patch the runner's two per-table drivers and the sorter for one run."""
    witness = RouteWitness()
    records: dict[int, SorterRecord] = {}
    real_stream, real_batch = _runner.stream_table, _runner._stream_table
    real_init = BoundedExternalSorter.__init__
    real_write_run = BoundedExternalSorter._write_run
    real_merge = BoundedExternalSorter._merge_group
    real_close = BoundedExternalSorter.close

    real_run = ooc_pkg.run_fk_out_of_core
    sized_keys = (
        "memory_limit",
        "batch_rows",
        "budget_bytes",
        "temp_disk_budget_bytes",
        "out_of_core_reorder_threshold_rows",
    )

    def run_fk(*a: Any, **kw: Any) -> Any:
        witness.runner_args = {k: kw.get(k) for k in sized_keys}
        return real_run(*a, **kw)

    def stream(**kw: Any) -> Any:
        witness.driver_by_table[kw["table_name"]] = "reorder"
        return real_stream(**kw)

    def batch(**kw: Any) -> Any:
        witness.driver_by_table[kw["table_name"]] = "batch"
        return real_batch(**kw)

    def init(
        self: BoundedExternalSorter,
        spill_dir: Path,
        run_bytes_cap: int,
        merge_fan_in: int,
        *a: Any,
        **k: Any,
    ) -> None:
        real_init(self, spill_dir, run_bytes_cap, merge_fan_in, *a, **k)
        rec = SorterRecord(run_bytes_cap=run_bytes_cap, merge_fan_in=merge_fan_in)
        records[id(self)] = rec
        witness.sorters.append(rec)

    def write_run(self: BoundedExternalSorter, table: Any, path: Path) -> None:
        real_write_run(self, table, path)
        rec = records[id(self)]
        rec.runs_written += 1
        rec.spilled_bytes += Path(path).stat().st_size

    def merge_group(self: BoundedExternalSorter, run_paths: list[Path], output: Path) -> None:
        real_merge(self, run_paths, output)
        records[id(self)].merge_groups += 1

    def close(self: BoundedExternalSorter) -> None:
        # Peaks are read at close: the sorter is drained by then and the properties are final.
        rec = records.get(id(self))
        if rec is not None:
            rec.peak_buffered_bytes = self.peak_buffered_bytes
            rec.peak_merge_resident_bytes = self.peak_merge_resident_bytes
        real_close(self)

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(ooc_pkg, "run_fk_out_of_core", run_fk))
        stack.enter_context(mock.patch.object(_runner, "stream_table", stream))
        stack.enter_context(mock.patch.object(_runner, "_stream_table", batch))
        stack.enter_context(mock.patch.object(BoundedExternalSorter, "__init__", init))
        stack.enter_context(mock.patch.object(BoundedExternalSorter, "_write_run", write_run))
        stack.enter_context(mock.patch.object(BoundedExternalSorter, "_merge_group", merge_group))
        stack.enter_context(mock.patch.object(BoundedExternalSorter, "close", close))
        yield witness
