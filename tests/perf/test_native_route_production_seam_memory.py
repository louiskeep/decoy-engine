"""Acceptance test 5 (docs/plans/2026-09-04-native-route-production-seam.md
section 4): flat peak RSS through the PRODUCTION entry with the streaming
Parquet sink, over row tiers, with lazy input and incremental output.

Each rep runs `scripts/native_route_seam_probe.py` in a fresh process (peak
RSS is the process's own `resource.getrusage(RUSAGE_SELF).ru_maxrss`, which
on Linux IS the kernel's VmHWM high-water mark -- no external polling
needed, matching the reasoning `test_out_of_core_memory_sentinel.py` uses
for its own subprocess isolation, just without that test's external-poll
step since a single-rep self-report is sufficient here). The fixture Parquet
file is built in a SEPARATE process invocation so building it never counts
toward the measured run's peak.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.perf

_PROBE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
    "native_route_seam_probe.py",
)

# 1x / 4x tiers. Measured 2026-09-04 on this box: ~268 MB at both tiers
# (ratio ~1.00) -- passthrough/redact/truncate over a LazySource input and a
# ParquetTransactionalSink output hold no per-row growing structure, so the
# only tier-dependent cost is a handful of resident batches at any one time.
_ROWS_1X = 500_000
_ROWS_4X = 2_000_000

# Comfortable headroom over the ~268 MB measured baseline (matches the
# out-of-core sentinel's own ~2x-measured-value convention): tight enough to
# catch an order-of-magnitude regression (e.g. a batch no longer evicted, or
# the whole source materialized), loose enough to absorb cross-machine
# allocator variance.
_PEAK_RSS_BUDGET_MB = 500.0

# The measured tier-to-tier ratio is ~1.00; 1.5x leaves room for allocator/
# fragmentation noise while still catching a real linear-in-rows regression.
_MAX_TIER_RATIO = 1.5


def _run_probe(*args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, _PROBE, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)  # noqa: S603
    assert proc.returncode == 0, f"probe subprocess failed: {proc.stderr}"
    return proc


def _tier(tmp_path_factory: pytest.TempPathFactory, n_rows: int) -> dict:
    work = tmp_path_factory.mktemp(f"native-seam-{n_rows}")
    src = work / "src.parquet"
    target = work / "out"
    _run_probe("build", str(src), str(n_rows))
    proc = _run_probe("run", str(src), str(target))
    for line in proc.stdout.strip().splitlines():
        if line.startswith("BENCH_JSON "):
            return json.loads(line[len("BENCH_JSON ") :])
    raise AssertionError(f"no BENCH_JSON in probe stdout:\n{proc.stdout}\n{proc.stderr}")


def test_native_route_peak_rss_under_budget(tmp_path_factory: pytest.TempPathFactory) -> None:
    rec = _tier(tmp_path_factory, _ROWS_1X)
    assert rec["native_admitted"] is True, rec["reroute_reason"]
    peak_mb = rec["peak_rss_kb"] / 1024
    assert peak_mb < _PEAK_RSS_BUDGET_MB, (
        f"native route peak RSS {peak_mb:.1f} MB exceeds the {_PEAK_RSS_BUDGET_MB:.0f} MB "
        f"budget at {_ROWS_1X:,} rows"
    )


def test_native_route_flat_across_row_count(tmp_path_factory: pytest.TempPathFactory) -> None:
    one_x = _tier(tmp_path_factory, _ROWS_1X)
    four_x = _tier(tmp_path_factory, _ROWS_4X)
    assert one_x["native_admitted"] is True, one_x["reroute_reason"]
    assert four_x["native_admitted"] is True, four_x["reroute_reason"]
    ratio = four_x["peak_rss_kb"] / one_x["peak_rss_kb"]
    assert ratio <= _MAX_TIER_RATIO, (
        f"peak RSS ratio {ratio:.2f} at {_ROWS_4X:,} rows vs {_ROWS_1X:,} rows exceeds the "
        f"{_MAX_TIER_RATIO:.1f}x flat-memory bound (1x={one_x['peak_rss_kb']} kB, "
        f"4x={four_x['peak_rss_kb']} kB)"
    )
