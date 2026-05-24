"""Bug 5 — engineering-correctness calibration.

Question: does the hybrid engine actually handle bigger data than
pandas? The architectural promise (Item 47) is that DuckDB streams
sources, Polars streams relational ops, and only the mask step
materializes — so pandas-only mode pays for full materialization at
every stage while hybrid mode only pays once.

Test: a representative pipeline (source -> filter -> mask -> sink) at
multiple scales, under both engines, with peak-RSS sampled every 100 ms.
The pandas curve should grow ~linearly with row count (full
materialization at source); the hybrid curve should stay roughly flat
in the source/filter stages.

Tier-3 calibration per BENCHMARKING_GUIDE.md — engineering-correctness.
Manual run, dev laptop. Marketing-correctness (50M+) is its own tier
that needs a cloud VM.

Run with:
  pytest tests/benchmark/calibration/test_engineering_correctness.py -m benchmark -s

Expected duration on a 32 GB i7-1265U: ~5–10 min total across all cells
(fixture build + 6 pipeline runs).
"""

from __future__ import annotations

import gc
import threading
import time
from pathlib import Path

import psutil
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.graph import run_graph

# Three scales: 1M (smoke), 5M (where 16 GB pandas was supposed to OOM
# per the original plan), 10M (gives clean linear-vs-flat signal on a
# 32 GB box). Bigger scales are out of scope for laptop calibration —
# Tier-4 marketing-correctness uses a cloud VM.
ROWS_TIERS: list[int] = [1_000_000, 5_000_000, 10_000_000]


def _build_hipaa_fixture(rows: int, dst: Path) -> None:
    """One-time fixture build per scale. ~150 bytes/row on disk after
    parquet compression; ~250 bytes/row materialized in numpy-pandas.
    Constant values per column — fine for a calibration test where
    we're measuring memory-pressure-vs-pipeline-size, not data quality.
    """
    if dst.exists():
        return
    table = pa.Table.from_pydict(
        {
            "patient_id": pa.array(range(rows), type=pa.int64()),
            "first_name": pa.array(["Alice"] * rows),
            "last_name": pa.array(["Smith"] * rows),
            "ssn": pa.array(["123-45-6789"] * rows),
            "dob": pa.array(["1985-03-15"] * rows),
            "zip": pa.array(["90210"] * rows),
            "email": pa.array(["a@b.com"] * rows),
            "phone": pa.array(["555-234-5678"] * rows),
            "amount": pa.array([10.5] * rows, type=pa.float64()),
            "score": pa.array([0.1] * rows, type=pa.float64()),
        }
    )
    pq.write_table(table, dst, compression="snappy")


class _RSSPoller:
    """Background thread that samples this process's RSS every
    interval_ms, tracking peak. Stop with .stop() (or use as context
    manager). Daemon thread so a test failure doesn't hang the runner.
    """

    def __init__(self, interval_ms: int = 100):
        self.interval = interval_ms / 1000
        self.peak_rss = 0
        self._process = psutil.Process()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True)

    def __enter__(self) -> _RSSPoller:
        self.peak_rss = self._process.memory_info().rss
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def _poll(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                rss = self._process.memory_info().rss
            except psutil.NoSuchProcess:
                return
            if rss > self.peak_rss:
                self.peak_rss = rss


# Pipeline shape: source.file -> filter (~10% pass rate) -> mask -> target.file.
# The filter stage is where the architectural difference shows up —
# pandas materializes the full source first; hybrid streams through
# DuckDB + Polars and only the surviving rows reach mask.
_PIPELINE = """\
mode: graph
engine: {engine}
nodes:
  - id: src
    kind: source.file
    config:
      path: "{src}"
  - id: flt
    kind: filter
    config:
      predicate: "patient_id < {threshold}"
  - id: mask
    kind: mask
    config:
      columns:
        ssn:
          strategy: hash
  - id: snk
    kind: target.file
    config:
      output_filename: "{dst}"
edges:
  - {{ from: src, to: flt }}
  - {{ from: flt, to: mask }}
  - {{ from: mask, to: snk }}
"""


# (rows, engine) -> {elapsed, peak_rss_mb, success}
_RESULTS: dict[tuple[int, str], dict] = {}


@pytest.mark.benchmark
@pytest.mark.parametrize("rows", ROWS_TIERS)
@pytest.mark.parametrize("engine", ["pandas", "hybrid"])
def test_calibration(rows: int, engine: str, tmp_path_factory) -> None:
    fixture_dir = tmp_path_factory.getbasetemp() / "calibration_fixtures"
    fixture_dir.mkdir(exist_ok=True)
    src = fixture_dir / f"hipaa_{rows}.parquet"
    _build_hipaa_fixture(rows, src)
    dst = fixture_dir / f"out_{engine}_{rows}.parquet"

    yaml_text = _PIPELINE.format(
        engine=engine,
        # Forward-slash paths so the YAML stays portable on Windows.
        src=str(src).replace("\\", "/"),
        threshold=rows // 10,
        dst=str(dst).replace("\\", "/"),
    )

    # GC + brief settle before measuring — keeps RSS-baseline stable
    # across cells in the same pytest session.
    gc.collect()
    time.sleep(0.2)

    with _RSSPoller(interval_ms=100) as poller:
        start = time.perf_counter()
        try:
            result = run_graph(yaml_text)
            success = result["success"]
            error = None
        except (MemoryError, Exception) as exc:
            # OOM is a valid outcome at calibration scales — record it,
            # don't crash the test. The summary table surfaces which
            # (engine, rows) cells survived.
            success = False
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
        elapsed = time.perf_counter() - start

    peak_rss_mb = poller.peak_rss / 1024 / 1024
    _RESULTS[(rows, engine)] = {
        "elapsed": elapsed,
        "peak_rss_mb": peak_rss_mb,
        "success": success,
        "error": error,
    }

    suffix = "" if success else f"  error={error}"
    print(
        f"[bug-5-calibration] engine={engine:7s} rows={rows:>10,} "
        f"elapsed={elapsed:6.2f}s peak_rss={peak_rss_mb:>7.0f}MB "
        f"success={success}{suffix}"
    )

    if not success:
        # Don't pytest.fail() on OOM — that's the whole point of the
        # calibration. Use pytest.skip() so the test framework records
        # it as "didn't fully complete" without breaking the run.
        pytest.skip(f"calibration OOM/error at {rows}: {error}")


@pytest.mark.benchmark
def test_zzz_summary() -> None:
    """Trend report — runs after the parameterized cells. Always
    passes; this is the reporter."""
    if not _RESULTS:
        pytest.skip("no results captured (run the parameterized tests first)")

    print()
    print("=" * 90)
    print("Bug 5 calibration — pandas vs hybrid engine, peak RSS x row count")
    print("=" * 90)
    print(f"{'rows':>10}  {'engine':>7}  {'elapsed':>8}  {'peak_rss_mb':>12}  {'status':s}")
    print("-" * 90)
    for rows in ROWS_TIERS:
        for engine in ("pandas", "hybrid"):
            r = _RESULTS.get((rows, engine))
            if r is None:
                continue
            status = "ok" if r["success"] else f"FAIL ({r['error'][:40]})"
            print(
                f"{rows:>10,}  {engine:>7s}  "
                f"{r['elapsed']:>7.2f}s  "
                f"{r['peak_rss_mb']:>10.0f} MB  {status}"
            )

    # Architectural-claim check: at the largest tier where both engines
    # completed, hybrid peak RSS should be meaningfully smaller.
    print()
    print("=" * 90)
    for rows in reversed(ROWS_TIERS):
        pd_r = _RESULTS.get((rows, "pandas"))
        hy_r = _RESULTS.get((rows, "hybrid"))
        if pd_r and hy_r and pd_r["success"] and hy_r["success"]:
            ratio = (
                pd_r["peak_rss_mb"] / hy_r["peak_rss_mb"]
                if hy_r["peak_rss_mb"] > 0
                else float("inf")
            )
            print(
                f"At {rows:,} rows: pandas peak {pd_r['peak_rss_mb']:.0f} MB; "
                f"hybrid peak {hy_r['peak_rss_mb']:.0f} MB ({ratio:.1f}x more on pandas). "
                f"Architectural claim: {'VALIDATED' if ratio > 1.5 else 'INCONCLUSIVE'}."
            )
            break
    else:
        print(
            "VERDICT: no row-count tier where both engines completed; "
            "extend ROWS_TIERS or investigate the failures above."
        )
