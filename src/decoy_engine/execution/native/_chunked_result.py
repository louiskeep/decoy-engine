"""Build one native chunk's `ExecutionResult` and read the oracle leg's preserved-row counts.

Split out of `_chunked_entry.py` (module-size budget).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution.native._chunked_evidence import ColumnPlan, chunk_route_evidence
from decoy_engine.execution.native._dispatch import NativeRouteEvidence
from decoy_engine.instrumentation.timing import StrategyTimingRecord


def _oracle_preserved_rows(quality_metrics: dict[str, Any]) -> dict[str, int]:
    """The oracle leg's D1c sentinel counts per column, from the handlers' own metrics."""
    return {e["column"]: e["preserved_rows"] for e in quality_metrics.get("date_preserve", ())}


def _native_chunk_result(
    *,
    table: str,
    out: pa.Table,
    warnings: tuple[Any, ...],
    elapsed_s: dict[str, float],
    col_seed_by_name: dict[str, Any],
    columns: tuple[ColumnPlan, ...],
    read_passthrough: tuple[str, ...],
    kernel_idle: set[str],
    row_errors: tuple[Any, ...],
    preserved_rows: dict[str, int],
    decision: NativeRouteEvidence,
) -> ExecutionResult:
    elapsed_ms = {col: s * 1000.0 for col, s in elapsed_s.items()}
    return ExecutionResult(
        outputs={table: out},
        timings=tuple(
            StrategyTimingRecord(
                strategy_type=col_seed_by_name[col].strategy,
                column=col,
                elapsed_ms=ms,
                peak_memory_delta_kb=0,
            )
            for col, ms in elapsed_ms.items()
        ),
        boundary_conversion_ms=0.0,
        warnings=warnings,
        quality_metrics={
            "chunked_route": chunk_route_evidence(
                table=table,
                native_admitted=True,
                reroute_reason=None,
                decline_records=decision.decline_records,
                strategy_branch_calls=decision.kernel_calls,
                columns=columns,
                elapsed_ms=elapsed_ms,
                pandas_read_passthrough=read_passthrough,
                kernel_idle_columns=kernel_idle,
                preserved_rows=preserved_rows,
            )
        },
        row_errors=row_errors,
    )
