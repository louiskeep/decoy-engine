"""Roll per-chunk results of the chunked mask route up into one full-frame-shaped result.

Split out of `_chunked.py` (module-size budget); `_chunked` re-exports both names.
"""

from __future__ import annotations

from typing import Any

__all__ = ["aggregate_chunk_timings", "aggregate_chunk_warnings"]


def aggregate_chunk_warnings(chunk_results: list[Any]) -> tuple[Any, ...]:
    """Order-stable union of per-chunk QualityWarnings.

    The same warning re-emitted by every chunk (they all run the same
    plan) collapses to one, exactly what the full-frame run would have
    emitted; genuinely distinct warnings keep first-emission order.
    Equality-based dedup because QualityWarning carries a dict detail
    (unhashable); warning counts are tiny, so O(n^2) is irrelevant.
    """
    seen: list[Any] = []
    for result in chunk_results:
        for warning in result.warnings:
            if warning not in seen:
                seen.append(warning)
    return tuple(seen)


def aggregate_chunk_timings(chunk_results: list[Any]) -> tuple[Any, ...]:
    """Coarse per-(strategy, column) rollup of per-chunk timing records.

    Elapsed times sum (the chunks really ran serially); memory deltas
    take the max, because per-chunk peaks measure the same bounded
    working set repeatedly -- summing them would fabricate a peak no run
    ever had. One record per (strategy, column), the same shape the
    full-frame result carries.
    """
    from decoy_engine.instrumentation.timing import StrategyTimingRecord

    order: list[tuple[str, str]] = []
    elapsed: dict[tuple[str, str], float] = {}
    peak: dict[tuple[str, str], int] = {}
    for result in chunk_results:
        for record in result.timings:
            key = (record.strategy_type, record.column)
            if key not in elapsed:
                order.append(key)
                elapsed[key] = 0.0
                peak[key] = 0
            elapsed[key] += record.elapsed_ms
            peak[key] = max(peak[key], record.peak_memory_delta_kb)
    return tuple(
        StrategyTimingRecord(
            strategy_type=strategy,
            column=column,
            elapsed_ms=elapsed[(strategy, column)],
            peak_memory_delta_kb=peak[(strategy, column)],
        )
        for strategy, column in order
    )
