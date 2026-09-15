"""Task 4.5 D9 PERFORMANCE: the CI-side smoke tier ONLY.

This is a fast, in-process, small-row-count paired comparison -- a
functional sanity check (the unified slice actually runs, its output
matches the legacy oracle, timing is in the right ballpark) at ~2,000 rows,
never the statistical claim D9 requires at 10k/100k/1M rows (>=20 alternating
trials, bootstrap CI, peak-RSS ratio via external process measurement).
That statistical claim is DEFERRED to a separate, not-yet-built follow-up
task, specified in:

    scripts/bench-unified-slice/README.md

The frozen workload substrate for that future harness
(`bench_worker_unified.py`) is committed and smoke-tested
(`test_bench_unified_slice_harness_smoke.py`), but the statistical
comparison driver itself is not built here: a >=20-rep 1M-row sweep is a
multi-minute-per-arm cost, inappropriate for every CI run, and the README
records the exact D9 requirements (tiers, warmups, per-rep pairing,
fail-closed RSS, thresholds) it must meet. That run is owed BEFORE Task 4.6
caller activation, not before merging this default-off engine lane. This
module's job is only to catch a gross correctness/perf regression fast, on
every test run.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution import run_pipeline
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.keyprovider import SecretKeyProvider
from tests.physical._shadow_helpers import build_config, write_read_only_fixture

ENGINE_VERSION = "unified-slice-perf-smoke-test"
_MASK_KEY = bytes(range(32))
_N_ROWS = 2_000
_WARMUPS = 1
_TRIALS = 5

_COLUMNS = [
    {"name": "p", "strategy": "passthrough"},
    {"name": "r", "strategy": "redact"},
    {"name": "tr", "strategy": "truncate", "provider_config": {"length": 3}},
]


def _build_source(n_rows: int) -> pa.Table:
    idx = list(range(n_rows))
    return pa.table(
        {
            "p": pa.array([f"val-{i}" for i in idx], type=pa.string()),
            "r": pa.array([f"secret-{i}" for i in idx], type=pa.string()),
            "tr": pa.array([f"code-{i:08d}" for i in idx], type=pa.string()),
        }
    )


def test_smoke_tier_output_matches_and_is_not_grossly_slower(tmp_path: Path) -> None:
    source = _build_source(_N_ROWS)
    path = write_read_only_fixture(tmp_path, source, "smoke")
    config = build_config(tmp_path, "t", path, _COLUMNS)

    def _timed(flag: bool) -> tuple[float, object]:
        key_provider = SecretKeyProvider(secret=_MASK_KEY, key_version="v1")
        src = pq.read_table(path)
        t0 = time.perf_counter()
        result = run_pipeline(
            config,
            {"t": src},
            engine_version=ENGINE_VERSION,
            key_provider=key_provider,
            auto_chunk=False,
            unified_slice_enabled=flag,
        )
        return time.perf_counter() - t0, result

    for _ in range(_WARMUPS):
        _timed(False)
        _timed(True)

    off_walls: list[float] = []
    on_walls: list[float] = []
    off_result = on_result = None
    for _ in range(_TRIALS):
        wall, result = _timed(False)
        off_walls.append(wall)
        off_result = result
        wall, result = _timed(True)
        on_walls.append(wall)
        on_result = result

    assert off_result is not None and on_result is not None
    assert QUALITY_METRICS_KEY in on_result.quality_metrics
    assert QUALITY_METRICS_KEY not in off_result.quality_metrics
    off_table = off_result.outputs["t"]
    on_table = on_result.outputs["t"]
    for name in off_table.column_names:
        assert off_table.column(name).to_pylist() == on_table.column(name).to_pylist()

    # A generous smoke-tier bound (2x median), not the D9 statistical bar --
    # this only catches a gross regression (an accidental O(n^2) path, a
    # forgotten re-derivation), leaving the tight 1.10x/1.15x claim to the
    # deferred benchmark spec's real statistical protocol (README.md).
    off_median = statistics.median(off_walls)
    on_median = statistics.median(on_walls)
    assert on_median <= max(off_median * 2.0, 0.05), (
        f"unified-slice smoke tier ran suspiciously slower than the oracle: "
        f"off_median={off_median:.4f}s on_median={on_median:.4f}s"
    )
