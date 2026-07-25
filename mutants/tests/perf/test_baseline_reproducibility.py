"""PERF.BASE.3 (V2): reproducibility test for the V2 baseline harness.

The committed engine-v2-baseline.json is a one-shot CI artifact. But
the harness (scripts/run_engine_v2_baseline.py) must be deterministic
enough that two independent runs on the same machine produce numbers
within a tolerable band; otherwise the compare_baselines.py gate is
meaningless noise.

We verify by running the harness twice on mid-band cells and asserting
the polars p50 values land within 3x of each other. The 3x bound is
deliberately loose: it catches harness-level breakage (wrong cell,
scheduler seizure) without false-flaking on laptop thermal noise or
shared-runner CPU contention.

Why mid-band cells (date_shift ~21ms, hash ~11ms) rather than cheap-
band (passthrough/redact at < 2ms)? At sub-2ms per operation the OS
clock granularity and GC pause noise dominate; a single background
interrupt inflates the ratio beyond 3x on a loaded machine. The mid-
band cells have enough signal to separate a broken harness from normal
jitter without crossing into the slow range that would make the test
suite expensive.

Pattern: subprocess-isolated dual-run with ratio assertion, per the
standard benchmark-harness verification method. Each run gets a fresh
Python interpreter to avoid module-level cache contamination.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.perf

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_HARNESS = _REPO_ROOT / "scripts" / "run_engine_v2_baseline.py"

# Mid-band strategies: fast enough to stay under ~10s for two double-
# runs, slow enough to clear OS clock granularity and GC noise.
# date_shift is ~21ms and hash is ~11ms on the small fixture (1k rows).
_REPRO_STRATEGIES = ("date_shift", "hash")


def _run_harness(strategy: str, tmp_out: Path) -> dict:  # type: ignore[type-arg]
    """Invoke the harness as a subprocess for one strategy on small tier.

    Subprocess isolation ensures each run starts with a fresh Python
    interpreter and no module-level cache from a prior run in the same
    pytest process. The --iterations 3 count keeps the dual-run under
    ~5 seconds per parametrize case on any reasonable machine.
    """
    cmd = [
        sys.executable,
        str(_HARNESS),
        "--tier",
        "small",
        "--strategy",
        strategy,
        "--iterations",
        "3",
        "--out",
        str(tmp_out),
    ]
    # S603 false positive: cmd is built from sys.executable and our own
    # checked-in script with a static flag list; no user input involved.
    result = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        pytest.fail(
            f"V2 baseline harness failed for {strategy!r}:\n"
            f"  stdout={result.stdout!r}\n"
            f"  stderr={result.stderr!r}"
        )
    payload = json.loads(tmp_out.read_text(encoding="utf-8"))
    cells = [r for r in payload["results"] if r["strategy"] == strategy]
    if not cells:
        pytest.fail(f"no results for {strategy!r} in harness output")
    return cells[0]


@pytest.mark.parametrize("strategy", _REPRO_STRATEGIES)
def test_two_runs_land_within_3x_on_polars_p50(strategy: str, tmp_path: Path) -> None:
    """Two independent harness runs must land within 3x on the shipped
    substrate (polars) p50. Catches harness-level breakage -- a stuck
    process, a wrong-cell timing, or a serialised-parallelism regression
    -- without false-flaking on normal OS jitter.

    3x is the same order-of-magnitude bound as the pre-rewrite V1 test
    (deleted in b9b73e1 alongside the V1 graph runner). A broken harness
    or a cold cache seizure routinely exceeds 10x; real jitter stays
    well under 2x on most machines.
    """
    out_a = tmp_path / "run_a.json"
    out_b = tmp_path / "run_b.json"

    cell_a = _run_harness(strategy, out_a)
    cell_b = _run_harness(strategy, out_b)

    p50_a = float(cell_a["polars"]["p50_ms"])
    p50_b = float(cell_b["polars"]["p50_ms"])

    assert p50_a > 0 and p50_b > 0, (
        f"{strategy!r}: harness produced zero p50_ms "
        f"(run_a={p50_a} run_b={p50_b}); expected positive sub-100ms readings"
    )

    larger = max(p50_a, p50_b)
    smaller = min(p50_a, p50_b)
    ratio = larger / smaller
    assert ratio < 3.0, (
        f"{strategy!r}: polars p50 ratio {ratio:.1f}x between runs "
        f"(a={p50_a:.2f}ms b={p50_b:.2f}ms); harness or machine state "
        f"too unstable -- expected < 3x normal jitter"
    )
