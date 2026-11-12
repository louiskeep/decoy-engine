"""F06 performance probe: per-cell kernels, batch memory, issue-heavy input, streamed quarantine.

Run:  python scripts/f06_perf_probe.py [--rows N] [--json out.json]

Every figure is measured on synthetic values. Budgets are floors/ceilings recorded in
`docs/plans/evidence/2026-10-30-f06-r2-evidence.md`; the script exits non-zero when one is missed,
so a regression is a failing command rather than a remembered number.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any

import pyarrow as pa

from decoy_engine.declared_casting import (
    DeclaredCastRun,
    LocalProtectedQuarantineSink,
    ProtectedQuarantineRuntime,
    compile_parser,
    descriptor_for,
)
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.plan._checks_declared_casting import compile_declared_casting_contract

# Valid specs and sample cells come from the catalog's own registered vectors, so the probe cannot
# drift from the formats it measures.
KERNEL_IDS = (
    "integer/ascii/v1",
    "decimal/plain/v1",
    "date/iso8601/v1",
    "cobol/overpunch-ascii-trailing/v1",
    "cobol/comp-3/v1",
    "currency/explicit/v1",
)
# Floors (cells/second), ceilings and ratios, set with wide margin against the slowest CI profile.
BUDGET = {
    "kernel_cells_per_s_min": 100_000,
    "batch_peak_mib_per_100k_rows_max": 120.0,
    "issue_heavy_cells_per_s_min": 50_000,
    "quarantine_rows_per_s_min": 5_000,
    "streamed_vs_whole_time_ratio_max": 3.0,
}


def _plan(policy: str, quarantine: bool = False) -> Any:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the repo's test helpers
    from tests.acceptance._f06_support import (
        INT64,
        ISO_DATE,
        PROTECTED_QUARANTINE,
        column,
        config,
        validate,
    )

    cols = [
        column("id"),
        column("n", logical_type="integer", parse_format=INT64, error_policy=policy),
        column("d", logical_type="date", parse_format=ISO_DATE, error_policy=policy),
    ]
    top = {"protected_quarantine": {**PROTECTED_QUARANTINE, "max_rows": 10**7, "max_bytes": 10**10}}
    cfg = validate(config(cols, source_path="/x.csv", top=top if quarantine else None))
    return compile_declared_casting_contract(cfg)


def kernel_rates(cells: int) -> dict[str, float]:
    """Cells per second through the compiled parser (the path the boundary uses)."""
    out: dict[str, float] = {}
    for fid in KERNEL_IDS:
        spec, sample, _ = descriptor_for(fid, "parse").vectors[0]
        parse = compile_parser(spec)
        values = [sample] * cells
        started = time.perf_counter()
        for v in values:
            if not parse(v).ok:
                raise AssertionError(fid)
        out[fid] = cells / (time.perf_counter() - started)
    return out


def _table(rows: int, bad_every: int) -> pa.Table:
    ids = [str(i) for i in range(rows)]
    n = ["x" if bad_every and i % bad_every == 0 else str(i % 1000) for i in range(rows)]
    d = ["bad" if bad_every and i % bad_every == 1 else "2026-02-01" for i in range(rows)]
    return pa.table({"id": ids, "n": n, "d": d})


def batch_memory(rows: int) -> float:
    run = DeclaredCastRun(_plan("null"), None)
    table = _table(rows, 0)
    tracemalloc.start()
    tracemalloc.reset_peak()
    run.cast_batch("people", table)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return peak / (1024 * 1024) / (rows / 100_000)


def issue_heavy(rows: int) -> float:
    run = DeclaredCastRun(_plan("null"), None)
    table = _table(rows, 2)  # one failing cell in every row
    started = time.perf_counter()
    run.cast_batch("people", table)
    return rows * 2 / (time.perf_counter() - started)


def quarantine(rows: int, chunk: int) -> dict[str, float]:
    result: dict[str, float] = {}
    table = _table(rows, 1)  # every row quarantined
    for label, size in (("whole", rows), ("streamed", chunk)):
        with tempfile.TemporaryDirectory() as root:
            sink = LocalProtectedQuarantineSink(
                Path(root) / "q", key_provider=SecretKeyProvider(secret=b"q" * 32)
            )
            run = DeclaredCastRun(
                _plan("quarantine", True), ProtectedQuarantineRuntime(sink, "perf", "run")
            )
            started = time.perf_counter()
            for start in range(0, rows, size):
                run.cast_batch("people", table.slice(start, size), offset=start)
            _, token = run.finish()
            result[label] = time.perf_counter() - started
            if token is None:
                raise AssertionError("no quarantine token")
    result["rows_per_s"] = rows / result["streamed"]
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=200_000)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    rows = args.rows
    kernels = kernel_rates(rows)
    q = quarantine(min(rows, 20_000), 1_000)
    report = {
        "python": platform.python_version(),
        "pyarrow": pa.__version__,
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "rows": rows,
        "kernel_cells_per_s": {k: round(v) for k, v in kernels.items()},
        "batch_peak_mib_per_100k_rows": round(batch_memory(min(rows, 100_000)), 2),
        "issue_heavy_cells_per_s": round(issue_heavy(min(rows, 100_000))),
        "quarantine_rows_per_s": round(q["rows_per_s"]),
        "quarantine_streamed_vs_whole_time_ratio": round(q["streamed"] / q["whole"], 2),
        "budget": BUDGET,
    }
    misses = [
        f"kernel {k} {v:.0f}/s < {BUDGET['kernel_cells_per_s_min']}"
        for k, v in kernels.items()
        if v < BUDGET["kernel_cells_per_s_min"]
    ]
    if report["batch_peak_mib_per_100k_rows"] > BUDGET["batch_peak_mib_per_100k_rows_max"]:
        misses.append("batch memory over budget")
    if report["issue_heavy_cells_per_s"] < BUDGET["issue_heavy_cells_per_s_min"]:
        misses.append("issue-heavy throughput under budget")
    if report["quarantine_rows_per_s"] < BUDGET["quarantine_rows_per_s_min"]:
        misses.append("streamed quarantine throughput under budget")
    if (
        report["quarantine_streamed_vs_whole_time_ratio"]
        > BUDGET["streamed_vs_whole_time_ratio_max"]
    ):
        misses.append("streamed quarantine slower than the whole-table batch by more than budget")
    report["budget_misses"] = misses
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.json:
        args.json.write_text(text + "\n")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
