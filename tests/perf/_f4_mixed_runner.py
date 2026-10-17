"""Run the F4 mixed capacity worker in a subprocess and parse its BENCH_JSON line.

Shared by the fast smoke test and the opt-in scale test so both drive the exact script
that produced the recorded evidence."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

WORKER = Path(__file__).resolve().parents[2] / "scripts" / "fk-scale" / "bench_worker_mixed.py"


def run_worker(args: list[str], *, timeout_s: float) -> dict[str, Any]:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(WORKER), *args],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH_JSON ")]
    assert len(lines) == 1, f"no BENCH_JSON line (rc={proc.returncode}): {proc.stderr[-800:]}"
    record: dict[str, Any] = json.loads(lines[0].removeprefix("BENCH_JSON "))
    return record
