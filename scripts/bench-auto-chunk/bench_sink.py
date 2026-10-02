"""Driver for the B6a incremental-sink merge benchmark (plan 2026-10-02, Design 10).

Frozen method: both route families (`native`, `oracle`) at 2,000,000 and 10,000,000 rows;
per cell, three discarded warmup rounds and twenty measured rounds; each round runs the
resident (`stream_chunked_output=False`) and the streamed configuration once, in an order
shuffled by `random.Random(20261002)`; one fresh subprocess per trial, one trial at a
time, every cell under `/home/cam/.cache/pytest-one.lock` (the machine-wide test lock, held for the whole cell);
`native_threads=1`, `OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`. Percentiles are
nearest-rank (index `ceil(q * n)` of the sorted values). Bars and ceilings are computed here
from the plan's text; this script never edits them.

Usage: bench_sink.py --python PY --out-dir DIR [--rows 2000000 10000000] [--rounds 20]
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import platform
import random
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
WORKER = HERE / "bench_worker_sink.py"
LOCK = "/home/cam/.cache/pytest-one.lock"
SEED = 20261002
GIB = 1024**3
MIB = 1024**2
ROW_GROUP_ROWS = 1_048_576
CEILING = {2_000_000: int(1.5 * GIB), 10_000_000: int(3.0 * GIB)}
SAFETY_ABORT_RESIDENT = 8 * GIB


def _cpu_model() -> str:
    with open("/proc/cpuinfo") as handle:
        return next(
            (ln.split(":", 1)[1].strip() for ln in handle if ln.startswith("model name")), "?"
        )


def nearest_rank(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def summarize(values: list[float]) -> dict[str, float]:
    return {"p50": nearest_rank(values, 0.5), "p95": nearest_rank(values, 0.95), "max": max(values)}


@contextlib.contextmanager
def exclusive_lock() -> Iterator[None]:
    """The machine-wide test lock, held for one whole cell so no other test run overlaps it."""
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_trial(py: str, args: list[str]) -> None:
    env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    subprocess.run([py, str(WORKER), *args], check=True, env=env)  # noqa: S603


def trial(
    py: str, workload: str, rows: int, source: Path, mode: str, root: Path, tag: str
) -> dict[str, Any]:
    workdir = root / f"w_{tag}"
    out = root / f"{tag}.json"
    shutil.rmtree(workdir, ignore_errors=True)
    run_trial(
        py,
        [
            "run",
            "--workload",
            workload,
            "--rows",
            str(rows),
            "--source",
            str(source),
            "--mode",
            mode,
            "--workdir",
            str(workdir),
            "--out",
            str(out),
        ],
    )
    evidence = json.loads(out.read_text())
    shutil.rmtree(workdir, ignore_errors=True)
    return evidence


def check_trial(ev: dict[str, Any], workload: str, rows: int, reference: str) -> list[str]:
    problems: list[str] = []
    if ev["staged_sha256"] != reference:
        problems.append("staged file differs from the reference bytes")
    if ev["staged_tables"] != ["t"]:
        problems.append(f"staged tables {ev['staged_tables']}")
    if workload == "native" and ev["native_admitted"] is not True:
        problems.append("native workload did not run native")
    if workload == "oracle" and ev["native_admitted"] is not False:
        problems.append("oracle workload did not reroute to the oracle")
    if ev["auto_chunk"].get("mode") != "chunked":
        problems.append("job did not route chunked")
    out = ev["auto_chunk"]["output"]
    if ev["mode"] == "streamed":
        if ev["outputs_streamed"] is not True or out["mode"] != "streamed":
            problems.append("run did not stream")
        if out["byte_cut_row_groups"] != 0:
            problems.append("a row group was cut by the byte cap")
        if out["row_groups"] != math.ceil(rows / ROW_GROUP_ROWS):
            problems.append(f"row_groups {out['row_groups']}")
        if ev["lifetime_peak_bytes"] > CEILING.get(rows, int(1.5 * GIB)):
            problems.append(f"lifetime peak {ev['lifetime_peak_bytes']} over the ceiling")
    else:
        if ev["outputs_streamed"] is not False or out["mode"] != "resident":
            problems.append("resident run streamed")
        if ev["lifetime_peak_bytes"] > SAFETY_ABORT_RESIDENT:
            problems.append("resident run over the 8 GiB safety abort")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--rows", type=int, nargs="+", default=[2_000_000, 10_000_000])
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--workloads", nargs="+", default=["native", "oracle"])
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    root = Path(args.work_root)
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    results: dict[str, Any] = {
        "seed": SEED,
        "rounds": args.rounds,
        "warmups": args.warmups,
        "platform": platform.platform(),
        "cpu": _cpu_model(),
        "cells": {},
    }
    failures: list[str] = []
    for workload in args.workloads:
        for rows in args.rows:
            with exclusive_lock():
                cell = f"{workload}_{rows}"
                source = root / f"{cell}.parquet"
                if not source.exists():
                    run_trial(
                        args.python,
                        [
                            "prepare",
                            "--workload",
                            workload,
                            "--rows",
                            str(rows),
                            "--path",
                            str(source),
                        ],
                    )
                ref = trial(args.python, workload, rows, source, "resident", root, f"{cell}_ref")
                reference = ref["staged_sha256"]
                samples: dict[str, list[dict[str, Any]]] = {"resident": [], "streamed": []}
                for rnd in range(args.warmups + args.rounds):
                    order = ["resident", "streamed"]
                    rng.shuffle(order)
                    for mode in order:
                        ev = trial(
                            args.python, workload, rows, source, mode, root, f"{cell}_{mode}"
                        )
                        if rnd < args.warmups:
                            continue
                        ev["round"] = rnd - args.warmups
                        ev["order"] = order
                        ev["problems"] = check_trial(ev, workload, rows, reference)
                        failures += [
                            f"{cell} {mode} round {ev['round']}: {p}" for p in ev["problems"]
                        ]
                        samples[mode].append(ev)
                        print(
                            cell,
                            mode,
                            rnd,
                            round(ev["wall_s"], 2),
                            ev["increment_bytes"] // MIB,
                            flush=True,
                        )
                summary = {
                    mode: {
                        "wall_s": summarize([e["wall_s"] for e in evs]),
                        "increment_bytes": summarize([e["increment_bytes"] for e in evs]),
                        "lifetime_peak_bytes": summarize([e["lifetime_peak_bytes"] for e in evs]),
                        "arrow_pool_max_bytes": summarize([e["arrow_pool_max_bytes"] for e in evs]),
                    }
                    for mode, evs in samples.items()
                }
                results["cells"][cell] = {
                    "workload": workload,
                    "rows": rows,
                    "reference_sha256": reference,
                    "source_nbytes": ref["source_nbytes"],
                    "output_nbytes": ref["output_nbytes_resident"],
                    "summary": summary,
                    "trials": samples,
                }
                (out_dir / "results.json").write_text(
                    json.dumps(results, indent=1, allow_nan=False)
                )
    bars: dict[str, Any] = {}
    for workload in args.workloads:
        for rows in args.rows:
            s = results["cells"][f"{workload}_{rows}"]["summary"]
            wall = {
                "p50_ratio": s["streamed"]["wall_s"]["p50"] / s["resident"]["wall_s"]["p50"],
                "p95_ratio": s["streamed"]["wall_s"]["p95"] / s["resident"]["wall_s"]["p95"],
            }
            bars[f"W_{workload}_{rows}"] = {
                **wall,
                "pass": wall["p50_ratio"] <= 1.10 and wall["p95_ratio"] <= 1.15,
            }
        if 2_000_000 in args.rows and 10_000_000 in args.rows:
            big = results["cells"][f"{workload}_10000000"]["summary"]
            small = results["cells"][f"{workload}_2000000"]["summary"]
            s10 = big["streamed"]["increment_bytes"]["p95"]
            s2 = small["streamed"]["increment_bytes"]["p95"]
            r10 = big["resident"]["increment_bytes"]["p50"]
            bars[f"M1_{workload}"] = {
                "streamed_p95_10M": s10,
                "resident_p50_10M": r10,
                "ratio": s10 / r10,
                "pass": s10 <= 0.25 * r10,
            }
            limit = max(1.25 * s2, s2 + 128 * MIB)
            bars[f"M2_{workload}"] = {
                "streamed_p95_10M": s10,
                "streamed_p95_2M": s2,
                "limit": limit,
                "pass": s10 <= limit,
            }
    bars["trial_problems"] = failures
    bars["all_pass"] = not failures and all(
        v["pass"] for k, v in bars.items() if isinstance(v, dict)
    )
    results["bars"] = bars
    (out_dir / "results.json").write_text(json.dumps(results, indent=1, allow_nan=False))
    print(json.dumps(bars, indent=1))
    return 0 if bars["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
