"""Memory sentinels for the Option 4 out-of-core FK route (S1.5, C4/C5).

Companion to `tests/perf/test_fk_memory_scaling.py` (the full-frame sentinel)
and `scripts/fk_memory_probe.py` (the heavy sweep and the capability driver).
The two fast tests are the small, default-gate guards; the opt-in
(`benchmark`-marked) tests below them are the scale measurements, including
the Sprint C5 capability proof this track exists for.

**State of the route (post C1-C3).** The route streams end to end: sources
enter as Parquet-backed `LazySource`s read in bounded batches, `_relation.py`
and `_join.py` stage bounded record batches into DuckDB (which owns the
O(rows) dedup/join with on-disk spill), and output flows batch-wise into a
`ParquetTransactionalSink`. No Python or Arrow structure is sized by table
cardinality on that path, and `scripts/fk_memory_probe.py --mode out_of_core`
measures exactly that shape (LazySource inputs, chunk-written fixture). The
capability yardstick is `test_out_of_core_completes_where_in_memory_routes_oom`
below: at a hard memory cap, the route COMPLETES a relational FK job that
OOMs both the full-frame and `run_sequential` routes.

**Why the fast comparison is still NOT "out-of-core uses less memory than
full-frame" at this tier.** At 5,000 rows/table the route's fixed per-run
overhead (a DuckDB connection per relation build and per join edge,
`out_of_core/_duckdb.py`) dominates before bounded streaming has anything to
amortize against: measured ~224 MB out-of-core vs ~196 MB full-frame
(ratio ~1.15) on this box, 2026-07-02, post-C3 streaming. That is a fixed
cost, not a scaling defect; the crossover and the capability win are proven
at scale by the benchmark tests. Asserting "below full-frame" here would make
the test wrong, not the code.

Both fast tests run the probe script as a subprocess (matching
`scripts/fk_memory_probe.py`'s own isolation pattern) so the reported peak RSS
(VmHWM, the worker's own resident high-water mark) reflects only this run, not
whatever pytest itself, or an earlier test in the same session, has already
resident. `tracemalloc` alone (no subprocess) was considered and rejected: it
only sees Python-object allocations, not the pyarrow/DuckDB C buffers that
dominate this route's memory, so it would not catch the regression this
sentinel exists to catch.
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
    "fk_memory_probe.py",
)

# Small enough to keep the default gate fast (~9s for the three subprocess runs
# the two fast tests below spawn: out_of_core once for the RSS budget test, then
# out_of_core + full for the ratio test); large enough to exercise a real DuckDB
# join/relation pass per FK edge
# rather than a degenerate single-row case.
_ROWS = 5_000
_WIDTH = 16
_ORPHAN_FRAC = 0.02
_POLICY = "preserve"

# Measured ~224 MB at this tier (2026-07-02, post-C3 streaming inputs; the
# pre-C3 resident-input probe measured ~217 MB, so the fixed DuckDB/connection
# overhead, not input residency, sets this floor). Budget is ~2x measured to
# absorb cross-machine variance in DuckDB's fixed per-connection overhead and
# pyarrow/pandas allocator behavior, while still catching an
# order-of-magnitude regression (e.g. a relation/join no longer evicting
# between FK edges).
_PEAK_RSS_BUDGET_MB = 450.0

# Measured ratio at this tier is ~1.15 (2026-07-02, post-C3: out_of_core
# ~224 MB vs full-frame ~196 MB; pre-C3 it was ~1.3). Out-of-core still costs
# more than full-frame HERE because the per-edge DuckDB overhead is fixed and
# 5k rows is too small to amortize it; see the module docstring. The 2.0x
# bound is kept from the pre-C3 sentinel: it leaves variance headroom while
# still catching a regression that makes the small-scale overhead materially
# worse (e.g. a leaked DuckDB connection or a reintroduced full-column copy).
_MAX_OVER_FULL_FRAME_RATIO = 2.0

# C5 capability operating point, tuned on the 8 GB dev box (2026-07-02, see
# docs/relationships-memory-scaling.md section 6.3): at 400k rows/table x 16
# payload columns, the resident working set of the in-memory routes measures
# ~1.4-1.5 GB (full) and ~1.1+ GB (sequential), while the streaming route
# peaks at ~430 MB RSS. A 1,024 MB RLIMIT_DATA cap therefore sits between the
# two with >= ~40% margin on both sides: the baselines hit the cap reliably
# and out-of-core clears it reliably.
_CAP_ROWS = 400_000
_CAP_MEM_MB = 1_024


def _assert_both_edges_checked(rec: dict) -> None:
    """Non-vacuous parity on BOTH FK edges: parent->child and the grandchild
    edge, whose parent relation is built from the child's rewritten staged
    keys and can therefore break independently of the first edge."""
    edges = rec.get("fk_rows_checked_by_edge") or {}
    assert len(edges) == 2, f"expected both FK edges in the parity sample, got {edges}"
    for edge, count in edges.items():
        assert count > 0, f"parity check was vacuous on {edge} (no FK links resolved)"


def _probe(mode: str) -> dict:
    cmd = [
        sys.executable,
        _PROBE,
        "--rows",
        str(_ROWS),
        "--width",
        str(_WIDTH),
        "--orphan-frac",
        str(_ORPHAN_FRAC),
        "--orphan-policy",
        _POLICY,
        "--mode",
        mode,
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert proc.returncode == 0, f"probe subprocess failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_out_of_core_peak_rss_under_budget():
    """Sentinel: out-of-core peak RSS stays under a documented absolute
    ceiling at a fast, modest tier. Catches an unbounded-growth regression
    (e.g. a relation or join no longer evicting per FK edge) independent of
    how it compares to the full-frame path."""
    rec = _probe("out_of_core")
    assert rec["parity"] == "ok", "out-of-core output diverged from the oracle on the FK sample"
    # Guard against a vacuous "ok": parity passes trivially if zero FK links are
    # actually checked, so require real links on BOTH edges (the grandchild edge
    # resolves against the child's rewritten staged keys, so it can break, or
    # come back empty, independently of a healthy parent->child edge).
    _assert_both_edges_checked(rec)
    assert rec["peak_rss_mb"] < _PEAK_RSS_BUDGET_MB, (
        f"out-of-core peak RSS {rec['peak_rss_mb']:.1f} MB exceeds the "
        f"{_PEAK_RSS_BUDGET_MB:.0f} MB budget at {_ROWS:,} rows/table x 3 tables"
    )


def test_out_of_core_overhead_over_full_frame_bounded_at_small_scale():
    """Regression guard, not a "lower than full-frame" claim (see module
    docstring: fixed per-edge DuckDB overhead dominates at this tier). Bounds
    how much WORSE out-of-core is allowed to be than full-frame at this small,
    fast tier, so a future change that widens the known overhead (e.g. an
    extra full-table materialization per FK edge) is caught here rather than
    only showing up in the heavy sweep."""
    ooc = _probe("out_of_core")
    full = _probe("full")
    ratio = ooc["peak_rss_mb"] / full["peak_rss_mb"]
    assert ratio < _MAX_OVER_FULL_FRAME_RATIO, (
        f"out-of-core/full-frame peak RSS ratio {ratio:.2f} at {_ROWS:,} rows/table "
        f"exceeds the {_MAX_OVER_FULL_FRAME_RATIO:.1f}x bound (out-of-core={ooc['peak_rss_mb']:.1f} "
        f"MB, full-frame={full['peak_rss_mb']:.1f} MB)"
    )


@pytest.mark.benchmark
def test_out_of_core_meaningfully_below_full_frame_at_scale():
    """Informational, opt-in (run via `pytest -m benchmark`): the peak-RSS gap
    at a tier where full-frame's whole-resident cost is large (1M rows/table,
    full-frame ~3.4 GB). Post-C3 the out-of-core probe streams its inputs from
    Parquet, so its peak is set by fixed overhead plus bounded batches, not by
    row count, and the gap is structural. Not in the default gate because a
    1M-row tier takes minutes per mode, not seconds; see
    scripts/fk_memory_probe.py for the full sweep across tiers."""
    rows = 1_000_000
    cmd_base = [
        sys.executable,
        _PROBE,
        "--rows",
        str(rows),
        "--width",
        str(_WIDTH),
        "--orphan-frac",
        str(_ORPHAN_FRAC),
        "--orphan-policy",
        _POLICY,
        "--json",
    ]
    ooc_proc = subprocess.run(  # noqa: S603
        [*cmd_base, "--mode", "out_of_core"], capture_output=True, text=True, timeout=600
    )
    full_proc = subprocess.run(  # noqa: S603
        [*cmd_base, "--mode", "full"], capture_output=True, text=True, timeout=600
    )
    assert ooc_proc.returncode == 0, ooc_proc.stderr
    assert full_proc.returncode == 0, full_proc.stderr
    ooc = json.loads(ooc_proc.stdout.strip().splitlines()[-1])
    full = json.loads(full_proc.stdout.strip().splitlines()[-1])

    assert ooc["parity"] == "ok"
    _assert_both_edges_checked(ooc)
    assert ooc["peak_rss_mb"] < full["peak_rss_mb"], (
        f"out-of-core ({ooc['peak_rss_mb']:.1f} MB) is not below full-frame "
        f"({full['peak_rss_mb']:.1f} MB) at {rows:,} rows/table"
    )


@pytest.mark.benchmark
def test_out_of_core_completes_where_in_memory_routes_oom():
    """Sprint C5 capability proof, opt-in (run via `pytest -m benchmark`): at
    a hard per-process memory cap (RLIMIT_DATA via `--mem-cap-mb`, allocator
    pinned so the rlimit tracks real usage), the full-frame and sequential
    routes must OOM on the shared on-disk FK chain while the out-of-core route
    completes with FK parity intact. Guards against a vacuous pass by
    requiring real resolved FK links and a memory-shaped failure for both
    baselines (a crash for any other reason fails the test). Memory-heavy and
    minutes-long by design; see the tuned operating point constants above."""
    cmd = [
        sys.executable,
        _PROBE,
        "--capability",
        "--rows",
        str(_CAP_ROWS),
        "--width",
        str(_WIDTH),
        "--orphan-frac",
        str(_ORPHAN_FRAC),
        "--orphan-policy",
        _POLICY,
        "--mem-cap-mb",
        str(_CAP_MEM_MB),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)  # noqa: S603
    assert proc.stdout.strip(), f"capability driver produced no output: {proc.stderr}"
    rec = json.loads(proc.stdout.strip().splitlines()[-1])["capability"]
    outcomes = rec["outcomes"]

    # The baselines must fail BECAUSE of memory (the driver classifies any
    # other crash as "failed"), and the streaming route must genuinely finish.
    assert outcomes["full"]["outcome"] == "oom", outcomes["full"]
    assert outcomes["sequential"]["outcome"] == "oom", outcomes["sequential"]
    ooc = outcomes["out_of_core"]
    assert ooc["outcome"] == "completed", ooc
    assert ooc["parity"] == "ok", "out-of-core completed but broke FK parity"
    _assert_both_edges_checked(ooc)
    assert rec["proven"] is True
    assert proc.returncode == 0, proc.stderr
