"""Phase 1 deliverable: measure Arrow → pandas conversion cost on STORM
scans. Result drives the Phase 2 STORM `NATIVE_ENGINE` declaration.

Decision rule from the architecture plan:
  - overhead < 10% → STORM stays NATIVE_ENGINE = "pandas" (the default).
  - overhead >= 10% → STORM declares NATIVE_ENGINE = "arrow" and consumes
    pyarrow.Table directly (column-level conversion only where pandas /
    scipy is required).

This is a benchmark, not a regression test. It prints the number; the
assertion only catches catastrophic regressions (>50%) so CI doesn't
silently bake in a future-broken Phase 1.
"""

from __future__ import annotations

import time

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.graph.conversion import arrow_to_engine
from decoy_engine.storm import run_storm


def _hipaa_shaped_fixture(rows: int) -> pa.Table:
    """A pyarrow.Table shaped like a typical HIPAA-ish customer table.

    Column mix: identifiers (numeric + string), names, SSN-like, phone,
    email, DOB string, zip, plus some plain numerics. Roughly the shape
    STORM tends to scan in production.
    """
    base = {
        "patient_id":  list(range(rows)),
        "first_name":  ["Alice", "Bob", "Carol", "Dave", "Eve"] * (rows // 5 + 1),
        "last_name":   ["Smith", "Jones", "Davis", "Martin", "Wilson"] * (rows // 5 + 1),
        "ssn":         ["123-45-6789", "555-12-3456", "111-22-3333", "444-55-6677", "222-99-1212"] * (rows // 5 + 1),
        "dob":         ["1985-03-15", "1990-07-22", "0001-01-01", "1972-11-08", "9999-12-31"] * (rows // 5 + 1),
        "zip":         ["90210", "10001", "60601", "94102", "02134"] * (rows // 5 + 1),
        "email":       ["a@b.com", "c@d.org", "e@f.io", "g@h.co", "i@j.net"] * (rows // 5 + 1),
        "phone":       ["555-234-5678", "555-345-6789", "555-456-7890", "555-567-8901", "555-678-9012"] * (rows // 5 + 1),
        "amount":      [10.5, 20.25, 30.75, 40.0, 50.5] * (rows // 5 + 1),
        "score":       [0.1, 0.5, 0.9, 0.3, 0.7] * (rows // 5 + 1),
    }
    truncated = {k: v[:rows] for k, v in base.items()}
    return pa.Table.from_pydict(truncated)


def _bench_pandas_baseline(table: pa.Table) -> float:
    """STORM scan starting from a pandas DataFrame already in memory."""
    df = table.to_pandas()
    start = time.perf_counter()
    run_storm(df, "bench")
    return time.perf_counter() - start


def _bench_arrow_boundary(table: pa.Table) -> float:
    """STORM scan starting from pyarrow.Table — measures the full
    Arrow → pandas conversion + scan."""
    start = time.perf_counter()
    df = arrow_to_engine(table, "pandas")
    run_storm(df, "bench")
    return time.perf_counter() - start


# Conservative fixture size — keeps the benchmark fast in CI while still
# making the conversion cost measurable. Larger fixtures (1M+ rows) belong
# in the manual benchmark harness mentioned in the architecture plan, not
# the default test run.
BENCHMARK_ROWS = 50_000


@pytest.mark.benchmark
def test_storm_arrow_boundary_overhead_is_recorded(capsys):
    """Print the overhead percent so it lands in the Phase 1 commit notes.

    Catastrophic-regression guard at 50% so a future change that doubles
    the conversion cost can't slip in unnoticed.
    """
    table = _hipaa_shaped_fixture(BENCHMARK_ROWS)

    # Warmup — first run pays import / JIT costs that distort the number.
    _bench_pandas_baseline(table)

    baseline = _bench_pandas_baseline(table)
    with_boundary = _bench_arrow_boundary(table)

    overhead_pct = (with_boundary - baseline) / baseline * 100 if baseline > 0 else 0.0
    msg = (
        f"\n[storm-arrow-bench] rows={BENCHMARK_ROWS} "
        f"baseline={baseline:.3f}s "
        f"with_arrow_boundary={with_boundary:.3f}s "
        f"overhead={overhead_pct:.1f}%"
    )
    print(msg)

    # Decision rule for Phase 2: see plan.
    if overhead_pct >= 10:
        print(
            "[storm-arrow-bench] DECISION: declare STORM NATIVE_ENGINE='arrow' "
            "in Phase 2 — overhead exceeds 10%."
        )
    else:
        print(
            "[storm-arrow-bench] DECISION: keep STORM NATIVE_ENGINE='pandas' "
            "in Phase 2 — overhead is within budget."
        )

    assert overhead_pct < 50, (
        f"Catastrophic Arrow→pandas overhead in STORM scans: {overhead_pct:.1f}%. "
        "Investigate before shipping."
    )
