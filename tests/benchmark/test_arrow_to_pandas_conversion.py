"""Bug 4 spike -- measures the Arrow->pandas conversion cost itself.

This is the OTHER half of the Bug 4 question. The transforms compat spike
(tests/benchmark/transforms/test_bug_4_arrow_pandas_compat.py) asks "do
transforms still work under arrow-backed pandas?" -- i.e. does the optimization
break anything?

This spike asks the dual question: "how big is the optimization?" -- i.e.
how much do we save by passing `types_mapper=pd.ArrowDtype` to
`pyarrow.Table.to_pandas()`?

The default `to_pandas()` copies every Arrow buffer into a fresh
numpy-backed pandas frame. That copy is O(rows x columns x bytes/cell).
Passing `types_mapper=pd.ArrowDtype` wraps the existing Arrow buffer
without copying -- supposedly zero-cost.

Measured at 10k / 100k / 1M / 5M to see how the savings scale. The actual
end-to-end win for the engine lives here, not in the per-transform deltas.

Run with: ``pytest tests/benchmark/test_arrow_to_pandas_conversion.py -m benchmark -s``
"""

from __future__ import annotations

import gc
import time

import pandas as pd
import pyarrow as pa
import pytest

ROW_COUNTS: list[int] = [10_000, 100_000, 1_000_000, 5_000_000]


def _hipaa_shaped_table(rows: int) -> pa.Table:
    """A pyarrow.Table shaped like a typical HIPAA-ish table -- same shape
    as the STORM benchmark fixture so numbers compare across the bench
    suite.

    Uses pa.array(...) directly to avoid the from_pydict path's overhead
    on million-plus row counts.
    """
    return pa.Table.from_pydict(
        {
            "patient_id": pa.array(range(rows), type=pa.int64()),
            "first_name": pa.array(["Alice"] * rows, type=pa.large_string()),
            "last_name": pa.array(["Smith"] * rows, type=pa.large_string()),
            "ssn": pa.array(["123-45-6789"] * rows, type=pa.string()),
            "dob": pa.array(["1985-03-15"] * rows, type=pa.string()),
            "zip": pa.array(["90210"] * rows, type=pa.string()),
            "email": pa.array(["a@b.com"] * rows, type=pa.string()),
            "phone": pa.array(["555-234-5678"] * rows, type=pa.string()),
            "amount": pa.array([10.5] * rows, type=pa.float64()),
            "score": pa.array([0.1] * rows, type=pa.float64()),
        }
    )


_RESULTS: dict[int, dict] = {}


@pytest.mark.benchmark
@pytest.mark.parametrize("rows", ROW_COUNTS)
def test_arrow_to_pandas_conversion(rows):
    """For a given row count, time both conversion paths.

    Each path runs warmup + measurement. GC is forced between runs so
    leftover Python objects from one path don't bias the other's timing.
    """
    table = _hipaa_shaped_table(rows)

    # Default path: numpy-backed pandas. Copies every column.
    table.to_pandas()  # warmup
    gc.collect()
    start = time.perf_counter()
    df_numpy = table.to_pandas()
    numpy_elapsed = time.perf_counter() - start

    # Arrow-backed path: zero-copy wrap (in theory).
    table.to_pandas(types_mapper=pd.ArrowDtype)  # warmup
    gc.collect()
    start = time.perf_counter()
    df_arrow = table.to_pandas(types_mapper=pd.ArrowDtype)
    arrow_elapsed = time.perf_counter() - start

    # Sanity: both paths produced same row count, same column set.
    assert len(df_numpy) == len(df_arrow) == rows
    assert set(df_numpy.columns) == set(df_arrow.columns)

    speedup = numpy_elapsed / arrow_elapsed if arrow_elapsed > 0 else float("inf")
    _RESULTS[rows] = {
        "numpy_elapsed": numpy_elapsed,
        "arrow_elapsed": arrow_elapsed,
        "speedup": speedup,
    }

    print(
        f"[arrow-to-pandas-bench] rows={rows:>9} "
        f"numpy={numpy_elapsed:.4f}s arrow={arrow_elapsed:.4f}s "
        f"speedup={speedup:.1f}x"
    )


@pytest.mark.benchmark
def test_zzz_summary():
    """Trend table -- does the speedup grow with scale, plateau, or shrink?
    Always passes; this is the reporter."""
    if not _RESULTS:
        pytest.skip("no results captured")

    print()
    print("=" * 80)
    print("Arrow -> pandas conversion -- numpy-backed vs types_mapper=pd.ArrowDtype")
    print("=" * 80)
    print(f"{'rows':>10}  {'numpy s':>10s}  {'arrow s':>10s}  {'speedup':>10s}  {'savings':s}")
    print("-" * 80)
    for rows in ROW_COUNTS:
        r = _RESULTS.get(rows)
        if not r:
            continue
        np_t = r["numpy_elapsed"]
        ar_t = r["arrow_elapsed"]
        savings_ms = (np_t - ar_t) * 1000
        print(
            f"{rows:>10,}  {np_t:>10.4f}  {ar_t:>10.4f}  "
            f"{r['speedup']:>9.1f}x  {savings_ms:>+8.1f} ms"
        )
    print("=" * 80)

    # Quick verdict: how much bigger does this get at scale?
    if len(_RESULTS) >= 2:
        smallest = ROW_COUNTS[0]
        largest = max(_RESULTS.keys())
        small_savings = (
            _RESULTS[smallest]["numpy_elapsed"] - _RESULTS[smallest]["arrow_elapsed"]
        ) * 1000
        large_savings = (
            _RESULTS[largest]["numpy_elapsed"] - _RESULTS[largest]["arrow_elapsed"]
        ) * 1000
        if large_savings > 100:
            print(
                f"At {largest:,} rows the conversion saves {large_savings:.0f} ms "
                f"per call (vs {small_savings:.0f} ms at {smallest:,}). "
                f"For pipelines that round-trip Arrow <-> pandas multiple times, "
                f"this compounds -- types_mapper is meaningfully load-bearing."
            )
