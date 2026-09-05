"""Acceptance test 6 (docs/plans/2026-09-05-native-route-wider-types.md
section 7): flat peak RSS for the WIDENED lane (int/bool/timestamp columns,
so the preflight actually runs its own full streaming pass) plus the two-read
benchmark against the single-read oracle-chunked baseline.

The flat-RSS half mirrors `test_native_route_production_seam_memory.py`
exactly (same subprocess-isolation reasoning; see that file's docstring),
against `native_route_wider_types_probe.py`'s widened fixture instead of
slice 1's utf8-only one -- the preflight's own accumulator (per-column
counters + a fixed-size hash state) must not grow with row count either.

The benchmark half runs in-process (no subprocess isolation needed; it times
wall clock and counts bytes read, not RSS). Evidence shape actually measured
and accepted (plan section 7 test 6): WARM-cache only, median + IQR over five
reps, against the resident oracle-CHUNKED baseline (the representative
single-read comparison for this lane -- see `_run_chunked_oracle` in the
production-seam suite for why the auto-chunk planner needs a resident source).
Cold-cache measurement is omitted deliberately: dropping the OS page cache
needs privileged access this test environment does not have, so both arms are
measured warm. Accept thresholds: warm-cache wall ratio at most 2.8x (an
owner-accepted regression bound, not a tuning target -- the two-read +
integrity-digest design is inherently >2x a single read, and the security
review preferred that digest over a private raw-PII spool), read amplification
at most 2.1x (two bounded reads plus digest overhead).
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.profile._readers import LazySource

pytestmark = pytest.mark.perf

_PROBE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
    "native_route_wider_types_probe.py",
)

# 1x / 4x tiers, same scale as the slice-1 seam memory test.
_ROWS_1X = 500_000
_ROWS_4X = 2_000_000

# Two full passes plus O(columns) digest state cost a little more than the
# slice-1 single-pass lane's own measured ~268 MB; the budget stays generous
# for the same cross-machine-allocator-noise reason that file's does.
_PEAK_RSS_BUDGET_MB = 600.0
_MAX_TIER_RATIO = 1.5

# Plan section 7 test 6's accept thresholds. The warm-wall ceiling is an
# owner-accepted regression bound, not a tuning target: the widened lane is
# opt-in and default-off, exists for memory-boundedness rather than speed, and
# its two-read + integrity-digest design is inherently >2x a single read. The
# security review preferred that digest over a private raw-PII spool, so the
# extra read is deliberate. Measured warm wall was ~1.4x the resident
# oracle-chunked baseline (the representative single-read comparison; the
# chunked route carries its own per-chunk overhead, so it is a closer wall
# reference than full_frame). The ceiling stays at 2.8x as a regression bound
# with generous headroom over the measured ~1.4x for cross-machine noise; a
# future breach reopens the spool-vs-digest decision rather than silently
# loosening it. Do not raise it to make a slow run pass.
_MAX_WARM_WALL_RATIO = 2.8
_MAX_READ_AMPLIFICATION = 2.1
_BENCH_REPS = 5
_BENCH_ROWS = 300_000


def _run_probe(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, _PROBE, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)  # noqa: S603
    assert proc.returncode == 0, f"probe subprocess failed: {proc.stderr}"
    return proc


def _tier(tmp_path_factory: pytest.TempPathFactory, n_rows: int) -> dict:
    work = tmp_path_factory.mktemp(f"native-wider-{n_rows}")
    src = work / "src.parquet"
    target = work / "out"
    _run_probe("build", str(src), str(n_rows))
    proc = _run_probe("run", str(src), str(target))
    for line in proc.stdout.strip().splitlines():
        if line.startswith("BENCH_JSON "):
            return json.loads(line[len("BENCH_JSON ") :])
    raise AssertionError(f"no BENCH_JSON in probe stdout:\n{proc.stdout}\n{proc.stderr}")


def test_native_route_wider_types_peak_rss_under_budget(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    rec = _tier(tmp_path_factory, _ROWS_1X)
    assert rec["native_admitted"] is True, rec["reroute_reason"]
    peak_mb = rec["peak_rss_kb"] / 1024
    assert peak_mb < _PEAK_RSS_BUDGET_MB, (
        f"widened native route peak RSS {peak_mb:.1f} MB exceeds the "
        f"{_PEAK_RSS_BUDGET_MB:.0f} MB budget at {_ROWS_1X:,} rows"
    )


def test_native_route_wider_types_flat_across_row_count(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    one_x = _tier(tmp_path_factory, _ROWS_1X)
    four_x = _tier(tmp_path_factory, _ROWS_4X)
    assert one_x["native_admitted"] is True, one_x["reroute_reason"]
    assert four_x["native_admitted"] is True, four_x["reroute_reason"]
    ratio = four_x["peak_rss_kb"] / one_x["peak_rss_kb"]
    assert ratio <= _MAX_TIER_RATIO, (
        f"widened peak RSS ratio {ratio:.2f} at {_ROWS_4X:,} rows vs "
        f"{_ROWS_1X:,} rows exceeds the {_MAX_TIER_RATIO:.1f}x flat-memory bound "
        f"(1x={one_x['peak_rss_kb']} kB, 4x={four_x['peak_rss_kb']} kB)"
    )


# ---------------------------------------------------------------------------
# Two-read benchmark vs the single-read oracle-chunked baseline
# ---------------------------------------------------------------------------


def _median_iqr(samples: list[float]) -> tuple[float, float]:
    """Median and interquartile range (Q3 - Q1) of the reps. Reporting the IQR
    alongside the median makes the run-to-run spread visible, so a wall ratio
    near the ceiling can be read as signal rather than one noisy sample."""
    median = statistics.median(samples)
    quartiles = statistics.quantiles(samples, n=4)
    return median, quartiles[2] - quartiles[0]


def _bench_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    work = tmp_path_factory.mktemp("native-wider-bench")
    path = work / "src.parquet"
    idx = range(_BENCH_ROWS)
    table = pa.table(
        {
            "pt_int": pa.array(list(idx), type=pa.int64()),
            "pt_bool": pa.array([i % 2 == 0 for i in idx], type=pa.bool_()),
            "pt_ts": pa.array([i * 1000 for i in idx], type=pa.timestamp("ms")),
        }
    )
    pq.write_table(table, path)
    return path


def _bench_config(path: Path, out_path: Path) -> dict:
    from decoy_engine.config import PipelineConfig

    raw = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
        "targets": {"t": {"type": "file", "format": "parquet", "path": str(out_path)}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "pt_int", "strategy": "passthrough"},
                    {"name": "pt_bool", "strategy": "passthrough"},
                    {"name": "pt_ts", "strategy": "passthrough"},
                ],
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _counting_lazy_source(path: Path, counter: list[int]) -> LazySource:
    """A real `LazySource` subclass (so `isinstance` checks elsewhere in the
    routing machinery still treat it as lazy) that adds one whole file size
    to `counter` per full read -- an honest, directly-measured proxy for read
    amplification: this lane's two full sequential passes cost exactly 2x a
    single-read baseline's bytes, by construction, not by estimate. The
    widened lane's preflight reads via iter_batches and its execution reads via
    open_batches, so BOTH are counted; counting only one would silently
    under-report the amplification as 1x."""

    class _CountingLazySource(LazySource):
        def iter_batches(self, batch_rows: int):
            counter[0] += self.path.stat().st_size
            return super().iter_batches(batch_rows)

        def open_batches(self, batch_rows: int):
            counter[0] += self.path.stat().st_size
            return super().open_batches(batch_rows)

    return _CountingLazySource(path=path)


def test_native_route_wider_types_two_read_benchmark(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    from decoy_engine.execution._pipeline import run_pipeline

    path = _bench_fixture(tmp_path_factory)
    # Warm the OS page cache identically for both arms before any timed rep.
    path.read_bytes()

    native_times: list[float] = []
    native_bytes = [0]
    for i in range(_BENCH_REPS):
        out_path = path.parent / f"native_out_{i}.parquet"
        counted = _counting_lazy_source(path, native_bytes)
        config = _bench_config(path, out_path)
        t0 = time.perf_counter()
        result = run_pipeline(
            config,
            {"t": counted},
            engine_version="wider-types-bench",
            native_route_enabled=True,
            execution_mode="auto",
        )
        native_times.append(time.perf_counter() - t0)
        assert result.native_route is not None and result.native_route.admitted is True

    # The oracle-CHUNKED baseline is the representative single-read comparison
    # (plan section 7 test 6), not full_frame. The auto-chunk planner's runtime
    # dtype-stability gate declines a LazySource, so the baseline is fed a
    # resident pa.Table exactly as the production-seam chunked oracle is
    # (`_run_chunked_oracle`) -- output-identical, and it exercises the chunked
    # masking path. One resident read per rep is the single-read reference: it
    # sits inside the timed region so this is an honest one-pass-vs-two-pass
    # wall comparison, and one file size per rep is the byte reference the
    # native lane's two counted reads are amplified against.
    baseline_times: list[float] = []
    baseline_bytes = [0]
    chunked_mode: str | None = None
    for i in range(_BENCH_REPS):
        out_path = path.parent / f"oracle_out_{i}.parquet"
        config = _bench_config(path, out_path)
        baseline_bytes[0] += path.stat().st_size
        t0 = time.perf_counter()
        oracle = run_pipeline(
            config,
            {"t": pq.read_table(path)},
            engine_version="wider-types-bench",
            substrate="pandas",
            execution_mode="auto",
            auto_chunk=True,
            auto_chunk_threshold_rows=1,
            native_route_enabled=False,
        )
        baseline_times.append(time.perf_counter() - t0)
        chunked_mode = oracle.quality_metrics["auto_chunk"]["mode"]
    assert chunked_mode == "chunked", (
        f"the oracle baseline did not chunk (auto_chunk mode={chunked_mode!r}); "
        "it would not be the intended single-read chunked comparison"
    )

    native_median, native_iqr = _median_iqr(native_times)
    baseline_median, baseline_iqr = _median_iqr(baseline_times)
    wall_ratio = native_median / baseline_median
    read_amplification = native_bytes[0] / baseline_bytes[0]

    # The lane does exactly two full sequential reads per rep (preflight +
    # execution) against the baseline's one, so the amplification is byte-exact
    # 2.0. Asserting equality, not just the ceiling below, catches a read that
    # stops being counted -- the bug that let this report 1.0 when execution
    # moved to open_batches while only iter_batches was counted.
    assert read_amplification == 2.0, (
        f"native lane must do exactly two full reads (got {read_amplification:.2f}); "
        "a read is likely uncounted"
    )

    detail = (
        f"[warm-cache, {_BENCH_REPS} reps] native median={native_median * 1000:.1f}ms "
        f"IQR={native_iqr * 1000:.1f}ms baseline(chunked) median="
        f"{baseline_median * 1000:.1f}ms IQR={baseline_iqr * 1000:.1f}ms "
        f"wall_ratio={wall_ratio:.2f} read_amplification={read_amplification:.2f}"
    )
    if wall_ratio > _MAX_WARM_WALL_RATIO or read_amplification > _MAX_READ_AMPLIFICATION:
        pytest.fail(
            f"two-read design breached its accept threshold ({detail}); switch to the "
            "private-spool alternative or record an explicit owner acceptance "
            "(plan section 4/7)."
        )
    else:
        # Not a hard requirement beyond the thresholds above, but keeps the
        # measured numbers visible in a normal (non-verbose) test run.
        print(detail)
