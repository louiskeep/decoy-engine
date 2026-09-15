"""Task 4.5 deferred perf-bench: the WORKER-CONTRACT smoke test.

The full statistical comparison harness (`bench_compare.py`) and the D9
performance certification are DEFERRED to a separate follow-up (see
`scripts/bench-unified-slice/README.md`), so this module no longer exercises a
comparison driver. What it DOES pin is that the frozen workload substrate,
`scripts/bench-unified-slice/bench_worker_unified.py`, is runnable end-to-end
and emits a real, non-fabricated record: one tiny subprocess run at a small row
count, asserting the worker actually activated the unified slice and reported
real per-strategy timings (not the hard-coded `hash_ms=0.0` a prior version
emitted) plus its workload fingerprint. The heavy multi-tier statistical sweep
is a deferred manual run per the README.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from decoy_engine.execution.native._companion_status import native_companion_status

ENGINE_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = ENGINE_ROOT / "scripts" / "bench-unified-slice"
VENV_PY = Path(sys.executable)
_WORKER_ENV = {**__import__("os").environ, "PYTHONPATH": str(ENGINE_ROOT / "src")}

_SMOKE_N_ROWS = 1_000


@pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable",
)
def test_worker_emits_real_positive_per_strategy_timing() -> None:
    """A real subprocess run at a tiny row count: proves the worker script is
    runnable end-to-end and that `hash_ms` (and every other per-strategy
    metric) is a real measured number, not the old hard-coded `0.0`.

    The worker activates the hash lane, which needs the compiled kernel, so this
    skips on the companion-absent legs and runs in the companion-present job.
    """
    proc = subprocess.run(  # noqa: S603 fixed local benchmark command, no untrusted input
        [str(VENV_PY), str(BENCH_DIR / "bench_worker_unified.py"), str(_SMOKE_N_ROWS)],
        cwd=str(ENGINE_ROOT),
        env=_WORKER_ENV,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH_JSON "))
    rec = json.loads(line[len("BENCH_JSON ") :])

    assert rec["n_rows"] == _SMOKE_N_ROWS
    assert rec["out_rows"] == _SMOKE_N_ROWS
    assert rec["unified_slice_activated"] is True
    assert rec["hash_cols"] == 3
    for key in ("hash_ms", "redact_ms", "truncate_ms", "passthrough_ms"):
        assert rec[key] > 0.0, f"{key} was not a real positive measurement: {rec}"
    # The worker records what it masked, so a future comparison harness can
    # assert the two arms ran one identical workload.
    fingerprint = rec["workload_fingerprint"]
    assert fingerprint["n_rows"] == _SMOKE_N_ROWS
    assert len(fingerprint["columns"]) == 9  # the frozen nine-column W2-minus-pt_ts shape
