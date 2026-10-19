"""Shared harness for the F-audit sweeps (test-side only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.generation import _faker_pool, _streamed
from decoy_engine.generation._streamed import GenerationResult, run_generation
from tests.parity import _f_audit_cells as cells
from tests.parity import _f_audit_matrix as mx
from tests.parity._fr0_support import partition_from_cuts, prepared_config


def prepare(cell: cells.Cell, mp: pytest.MonkeyPatch) -> None:
    """Apply the cell's threshold and injected refusal before anything is built."""
    if cell.threshold is not None:
        mp.setattr(_faker_pool, "N_THRESHOLD", cell.threshold)
    if cell.inject is not None:
        cell.inject(mp)


def forbid_legacy(mp: pytest.MonkeyPatch) -> None:
    """Fail on ENTRY to the legacy whole-table path, before any value is produced."""

    def entered(*a: Any, **kw: Any) -> None:
        raise AssertionError("an admitted fixture entered the legacy whole-table path")

    mp.setattr(_streamed, "_run_legacy", entered)


def run_cell(
    cell: cells.Cell, route: str, tmp_path: Path
) -> tuple[GenerationResult, dict[str, pa.Table]]:
    """The cell through `run_generation` over irregular partitions, on one route."""
    config = prepared_config(cell.build())
    partition = partition_from_cuts({t["name"]: cells.CUTS for t in config["tables"]})
    kw: dict[str, Any] = {"partition": partition, "provider_snapshot": cell.provider_snapshot}
    if route == mx.RESIDENT:
        result = run_generation(config, **kw)
        assert result.tables is not None
        return result, result.tables
    target = tmp_path / "out"
    result = run_generation(config, sink=ParquetTransactionalSink(target), **kw)
    committed = {p.stem: pq.read_table(p) for p in sorted(target.glob("*.parquet"))}
    return result, committed


class BoundedSink:
    """A bounded sink that records any write: a legacy plan must leave it untouched."""

    bounded_write_batches = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def write(self, *a: Any, **kw: Any) -> None:
        self.calls.append("write")

    def write_batches(self, *a: Any, **kw: Any) -> None:
        self.calls.append("write_batches")

    def commit(self) -> None:
        self.calls.append("commit")

    def abort(self) -> None:
        self.calls.append("abort")
