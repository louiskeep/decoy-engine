"""Bug 4 spike -- does Arrow-backed pandas work with the masker transforms?

Question: `graph/conversion.py` skips `types_mapper=pd.ArrowDtype` because
the dev's comment says masker / faker code "assumes legacy numpy-backed
dtypes." Is that still true, or was the dev being conservative?

Method: build a fixture across multiple row counts (10k -> 5M) twice -- once
with default numpy-backed dtypes, once with Arrow-backed dtypes -- and run
every public transform strategy against both. For each (transform, backend,
rows) cell:

  - Does it run without raising?
  - Does the output shape match (length, no unexpected nulls)?
  - Is it faster / slower / same?

Decision matrix in plans/2026-05-09-hybrid-engine-bug-followup.md §Bug 4:

  - All work + faster on Arrow-dtype  -> flip default
  - Mixed (some break, some don't)    -> opt-in flag
  - All break or slower               -> leave the comment, no change

Run with: ``pytest tests/benchmark/transforms/test_bug_4_arrow_pandas_compat.py -m benchmark -s``
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd
import pytest

from decoy_engine.transforms import (
    DateShiftStrategy,
    FakerStrategy,
    FPEStrategy,
    HashStrategy,
    MapStrategy,
    PassthroughStrategy,
    RedactStrategy,
    ShuffleStrategy,
)


# Multi-scale: smoke / standard / large / scaled. The 5M tier needs ~32 GB
# RAM to comfortably hold both backends. Faker is too slow per-row to run
# above 100k in reasonable time, so we skip those cells.
ROW_COUNTS: list[int] = [10_000, 100_000, 1_000_000, 5_000_000]


# Cap a transform/backend/rows combo with this many seconds of expected
# runtime. Keeps the spike total under ~10 minutes instead of multi-hour.
SKIP_RULES: dict[tuple[str, str, int], str] = {
    # Faker: ~10ms per row in keyed mode -> 1M rows = ~10 minutes per cell.
    ("faker", "numpy", 1_000_000): "faker too slow at 1M rows; data point at 100k is sufficient",
    ("faker", "arrow", 1_000_000): "faker too slow at 1M rows; data point at 100k is sufficient",
    ("faker", "numpy", 5_000_000): "faker too slow at 5M rows",
    ("faker", "arrow", 5_000_000): "faker too slow at 5M rows",
    # Shuffle on arrow scales catastrophically (640x at 100k = 63s; at 1M
    # would project to ~10+ minutes; at 5M to hours). Already pathological;
    # one slow data point at 100k is enough to make the verdict.
    ("shuffle", "arrow", 1_000_000): "shuffle/arrow already catastrophic at 100k; skip larger",
    ("shuffle", "arrow", 5_000_000): "shuffle/arrow already catastrophic at 100k; skip larger",
}


# ── Fixture builders ────────────────────────────────────────────────────────

def build_string_column(rows: int, backend: str) -> pd.Series:
    values = [f"user{i}@example.com" for i in range(rows)]
    if backend == "arrow":
        return pd.Series(values, dtype="string[pyarrow]")
    return pd.Series(values, dtype="object")


def build_low_cardinality_string(rows: int, backend: str) -> pd.Series:
    """For map strategy -- needs a small set of distinct values."""
    pool = ["alice", "bob", "carol", "dave", "eve"]
    values = [pool[i % len(pool)] for i in range(rows)]
    if backend == "arrow":
        return pd.Series(values, dtype="string[pyarrow]")
    return pd.Series(values, dtype="object")


def build_date_column(rows: int, backend: str) -> pd.Series:
    """Cap day spread to 30 years so even 5M rows stay under pandas'
    nanosecond range (~year 2262). Repeated dates are fine for benchmarking
    -- the transform doesn't care about uniqueness."""
    base = datetime(1990, 1, 1)
    SPREAD_DAYS = 10_950  # ~30 years
    values = [base + timedelta(days=i % SPREAD_DAYS) for i in range(rows)]
    if backend == "arrow":
        return pd.Series(values, dtype="timestamp[ns][pyarrow]")
    return pd.Series(values, dtype="datetime64[ns]")


def build_fpe_column(rows: int, backend: str) -> pd.Series:
    """FPE expects digit strings of fixed length."""
    values = [f"{i:016d}" for i in range(rows)]
    if backend == "arrow":
        return pd.Series(values, dtype="string[pyarrow]")
    return pd.Series(values, dtype="object")


# ── Scenarios -- (name, strategy_class, rule, fixture_builder) ───────────────

SCENARIOS: list[tuple[str, type, dict, Callable[[int, str], pd.Series]]] = [
    (
        "faker",
        FakerStrategy,
        {"type": "faker", "column": "email", "faker_type": "email"},
        build_string_column,
    ),
    (
        "hash",
        HashStrategy,
        {"type": "hash", "column": "id"},
        build_string_column,
    ),
    (
        "redact",
        RedactStrategy,
        {"type": "redact", "column": "name", "value": "REDACTED"},
        build_string_column,
    ),
    (
        "map",
        MapStrategy,
        {"type": "map", "column": "label", "mapping": {
            "alice": "A", "bob": "B", "carol": "C", "dave": "D", "eve": "E",
        }},
        build_low_cardinality_string,
    ),
    (
        "shuffle",
        ShuffleStrategy,
        {"type": "shuffle", "column": "value"},
        build_string_column,
    ),
    (
        "passthrough",
        PassthroughStrategy,
        {"type": "passthrough", "column": "any"},
        build_string_column,
    ),
    (
        "date_shift",
        DateShiftStrategy,
        {"type": "date_shift", "column": "dob", "days": 30},
        build_date_column,
    ),
    (
        "fpe",
        FPEStrategy,
        {"type": "fpe", "column": "ccn", "alphabet": "0123456789", "tweak": "test"},
        build_fpe_column,
    ),
]


# (scenario_name, backend, rows) -> {ok, elapsed, error}
_RESULTS: dict[tuple[str, str, int], dict] = {}


@pytest.mark.benchmark
@pytest.mark.parametrize("rows", ROW_COUNTS)
@pytest.mark.parametrize("backend", ["numpy", "arrow"])
@pytest.mark.parametrize("name,strategy_cls,rule,builder", SCENARIOS,
                         ids=[s[0] for s in SCENARIOS])
def test_transform_under_dtype_backend(name, strategy_cls, rule, builder, backend, rows):
    """Per-cell test in the (transform x backend x rows) matrix.

    Pre-skip known-slow combos via SKIP_RULES so the spike doesn't take
    hours. Records into _RESULTS so the summary test can print the full
    grid even if individual cells fail.
    """
    skip_reason = SKIP_RULES.get((name, backend, rows))
    if skip_reason:
        pytest.skip(skip_reason)

    column = builder(rows, backend)
    strategy = strategy_cls()

    # Warmup pays JIT / Faker-init / first-import costs that distort the
    # measured run. Discarded.
    try:
        strategy.apply(column, rule)
    except Exception as exc:
        _RESULTS[(name, backend, rows)] = {
            "ok": False,
            "elapsed": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
        pytest.fail(f"{name}/{backend}/{rows} crashed in warmup: {exc}", pytrace=False)

    start = time.perf_counter()
    result = strategy.apply(column, rule)
    elapsed = time.perf_counter() - start

    _RESULTS[(name, backend, rows)] = {
        "ok": True,
        "elapsed": elapsed,
        "error": None,
    }

    assert isinstance(result, pd.Series), (
        f"{name}/{backend}/{rows} returned {type(result).__name__}, expected Series"
    )
    assert len(result) == len(column), (
        f"{name}/{backend}/{rows} returned {len(result)} rows, expected {len(column)}"
    )

    print(
        f"[bug-4-bench] strategy={name:12s} backend={backend:5s} "
        f"rows={rows:>9} elapsed={elapsed:.4f}s"
    )


@pytest.mark.benchmark
def test_zzz_summary():
    """Runs after the parameterized cells (alphabetical sort puts
    `zzz_summary` last). Prints a side-by-side trend table + the verdict
    per the bug-followup plan's matrix.

    Always passes -- this isn't a real test, just a reporter.
    """
    if not _RESULTS:
        pytest.skip("no results captured (run the parameterized tests first)")

    print()
    print("=" * 100)
    print("Bug 4 spike -- Arrow-backed pandas vs numpy-backed pandas (multi-scale)")
    print("=" * 100)

    # Per-scale tables -- easier to scan than one giant matrix.
    for rows in ROW_COUNTS:
        any_at_scale = any(
            (name, backend, rows) in _RESULTS
            for name in [s[0] for s in SCENARIOS]
            for backend in ("numpy", "arrow")
        )
        if not any_at_scale:
            continue
        print(f"\n--- {rows:,} rows ---")
        print(f"{'transform':12s} {'numpy ok':>9s} {'numpy s':>10s} "
              f"{'arrow ok':>9s} {'arrow s':>10s}  {'verdict':s}")
        print("-" * 80)
        for scenario in SCENARIOS:
            name = scenario[0]
            np_r = _RESULTS.get((name, "numpy", rows), {})
            ar_r = _RESULTS.get((name, "arrow", rows), {})
            if not np_r and not ar_r:
                continue  # Both skipped at this scale
            np_ok = np_r.get("ok", False)
            ar_ok = ar_r.get("ok", False)
            np_t = np_r.get("elapsed")
            ar_t = ar_r.get("elapsed")
            np_str = f"{np_t:.4f}" if np_t else ("skip" if not np_r else "--")
            ar_str = f"{ar_t:.4f}" if ar_t else ("skip" if not ar_r else "--")

            if np_t and ar_t:
                ratio = ar_t / np_t
                if ratio > 5.0:
                    verdict = f"!! ARROW {ratio:.0f}x SLOWER"
                elif ratio > 1.05:
                    verdict = f"arrow +{(ratio - 1) * 100:.0f}% slower"
                elif ratio < 0.95:
                    verdict = f"arrow -{(1 - ratio) * 100:.0f}% faster"
                else:
                    verdict = "~ same"
            else:
                verdict = "--"

            print(f"{name:12s} {str(np_ok):>9s} {np_str:>10s} "
                  f"{str(ar_ok):>9s} {ar_str:>10s}  {verdict}")

    # Cross-scale trend table for the headline transforms.
    print()
    print("=" * 100)
    print("Trend: arrow/numpy ratio per transform across scales")
    print("=" * 100)
    header = f"{'transform':12s} " + "".join(f"{r:>14,}" for r in ROW_COUNTS)
    print(header)
    print("-" * len(header))
    for scenario in SCENARIOS:
        name = scenario[0]
        cells = []
        for rows in ROW_COUNTS:
            np_r = _RESULTS.get((name, "numpy", rows), {})
            ar_r = _RESULTS.get((name, "arrow", rows), {})
            np_t = np_r.get("elapsed")
            ar_t = ar_r.get("elapsed")
            if np_t and ar_t and np_t > 0:
                cells.append(f"{ar_t / np_t:>14.2f}x")
            else:
                cells.append(f"{'--':>14s}")
        print(f"{name:12s} " + "".join(cells))

    # Verdict.
    print()
    print("=" * 100)
    catastrophic_combos = [
        (n, b, r) for (n, b, r), v in _RESULTS.items()
        if v.get("elapsed") and v["elapsed"] > 10 * (
            _RESULTS.get((n, "numpy" if b == "arrow" else "arrow", r), {}).get("elapsed") or 1
        )
    ]
    if catastrophic_combos:
        broken_names = sorted(set(n for n, _, _ in catastrophic_combos))
        print(f"VERDICT: Arrow-backed pandas works for most transforms but is "
              f"CATASTROPHICALLY slower for: {', '.join(broken_names)}.")
        print("-> ADD OPT-IN FLAG; default stays numpy. Document the broken transforms;")
        print("  callers can opt in only when their pipeline doesn't include the broken ones.")
    else:
        print("VERDICT: Arrow-backed pandas works across all transforms; details in trend table above.")
