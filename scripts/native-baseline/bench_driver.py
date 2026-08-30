"""W2 baseline driver: spawns a bench worker once per rep in a fresh process.

Each rep runs under /usr/bin/time -v so peak RSS is measured externally
(whole-process, never read from inside the measured process). Per tier:
1 discarded warmup, then N timed reps. Emits a JSON results blob to stdout
and a human summary to stderr.

`--worker` selects which worker script runs (default `bench_worker.py`, the
pinned pandas oracle from Task 2.0); pass `bench_worker_native.py` for the
Task 2.7 native-route measurement. Both scripts share the same BENCH_JSON /
VmHWM contract this driver reads, so one harness serves both.

Usage:
  python bench_driver.py --tiers 1000000,4000000 --reps 5 --out results.json
  python bench_driver.py --tiers 16000000 --reps 3 --out results_16x.json
  python bench_driver.py --worker bench_worker_native.py --prebuild build_w2_parquet.py \\
      --batch-rows 50000 --tiers 1000000,4000000,16000000 --reps 5 --out results_native.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The repo runs from multiple git worktrees sharing one venv (see
# CONTRIBUTING.md); self-locate the source tree from this script's own path
# rather than hardcoding the base checkout, so a run from a worktree measures
# THAT worktree's code, not whichever worktree the venv's editable install
# last pointed at.
ENGINE = HERE.parent.parent
VENV_PY = Path("/home/cam/vscode/decoy-engine/.venv/bin/python")
_WORKER_ENV = {**os.environ, "PYTHONPATH": str(ENGINE / "src")}

_JSON_RE = re.compile(r"^BENCH_JSON (.*)$", re.MULTILINE)
_HWM_RE = re.compile(r"^VmHWM:\s*(\d+)\s*kB", re.MULTILINE)


def _read_vmhwm_kb(pid: int) -> int | None:
    """Peak RSS high-water mark (kB) from /proc/<pid>/status; None if gone."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            m = _HWM_RE.search(fh.read())
            return int(m.group(1)) if m else None
    except (FileNotFoundError, ProcessLookupError, OSError):
        return None


def run_rep(n_rows: int, worker: Path, worker_args: list[str]) -> dict:
    """One rep in a fresh process; parent samples the child's peak RSS.

    RSS is measured EXTERNALLY: the parent polls the child's VmHWM (a
    monotonic peak) from /proc, so nothing is read from inside the measured
    process. The worker runs the pipeline in-process (no isolated/subprocess
    execution inside the engine); a fresh OS process per rep gives clean RSS.
    """
    cmd = [str(VENV_PY), str(worker), str(n_rows), *worker_args]
    t0 = time.time()
    proc = subprocess.Popen(  # noqa: S603 fixed local benchmark command, no untrusted input
        cmd,
        cwd=str(ENGINE),
        env=_WORKER_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    peak_rss_kb = 0
    while proc.poll() is None:
        hwm = _read_vmhwm_kb(proc.pid)
        if hwm is not None and hwm > peak_rss_kb:
            peak_rss_kb = hwm
        time.sleep(0.02)
    # Final read in case the last increase landed between polls.
    hwm = _read_vmhwm_kb(proc.pid)
    if hwm is not None and hwm > peak_rss_kb:
        peak_rss_kb = hwm

    stdout, stderr = proc.communicate()
    wall_outer = time.time() - t0
    if proc.returncode != 0:
        sys.stderr.write(stdout + "\n" + stderr + "\n")
        raise RuntimeError(f"rep failed (rc={proc.returncode}) at n_rows={n_rows}")

    m = _JSON_RE.search(stdout)
    if not m:
        raise RuntimeError(f"no BENCH_JSON in worker stdout:\n{stdout}")
    rec = json.loads(m.group(1))
    rec["peak_rss_kb"] = peak_rss_kb or None
    rec["wall_outer_s"] = wall_outer
    return rec


def summarize(n_rows: int, reps: list[dict]) -> dict:
    walls = sorted(r["wall_s"] for r in reps)
    rss = [r["peak_rss_kb"] for r in reps if r["peak_rss_kb"] is not None]
    # keyed-hash throughput: per-hash-column rows/sec, from in-process timings.
    hash_tputs = []
    for r in reps:
        if r.get("hash_ms") and r.get("hash_cols"):
            per_col_s = (r["hash_ms"] / r["hash_cols"]) / 1000.0
            hash_tputs.append(n_rows / per_col_s)
    q = statistics.quantiles(walls, n=4, method="inclusive") if len(walls) >= 2 else [walls[0]] * 3
    return {
        "n_rows": n_rows,
        "reps": len(reps),
        "wall_median_s": statistics.median(walls),
        "wall_q1_s": q[0],
        "wall_q3_s": q[2],
        "wall_iqr_s": q[2] - q[0],
        "wall_p95of_s": max(walls),
        "wall_min_s": min(walls),
        "peak_rss_max_kb": max(rss) if rss else None,
        "peak_rss_max_mb": round(max(rss) / 1024, 1) if rss else None,
        "hash_tput_median_rows_s": statistics.median(hash_tputs) if hash_tputs else None,
        "whole_job_tput_median_rows_s": n_rows / statistics.median(walls),
        "execution_mode": reps[0].get("execution_mode"),
        "raw_reps": reps,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", required=True, help="comma-separated row counts")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--worker",
        default="bench_worker.py",
        help="worker script (relative to this file's dir); default is the pinned oracle",
    )
    ap.add_argument(
        "--batch-rows",
        type=int,
        default=None,
        help="native worker's streaming batch size (also the --prebuild writer's row-group size)",
    )
    ap.add_argument(
        "--prebuild",
        default=None,
        help=(
            "script (relative to this dir) that builds a per-tier Parquet source ONCE, in a "
            "SEPARATE non-sampled process, before that tier's warmup+reps; its output path is "
            "passed to every rep as the worker's first extra arg (native worker's source-file arg)"
        ),
    )
    ap.add_argument(
        "--source-dir",
        default=None,
        help="directory for --prebuild's per-tier source files (default: this file's dir)",
    )
    args = ap.parse_args()

    worker = HERE / args.worker
    prebuild = HERE / args.prebuild if args.prebuild else None
    source_dir = Path(args.source_dir) if args.source_dir else HERE

    tiers = [int(x) for x in args.tiers.split(",")]
    all_results = {}
    for n_rows in tiers:
        sys.stderr.write(
            f"\n=== TIER {n_rows} rows: {args.warmup} warmup + {args.reps} timed "
            f"(worker={args.worker}) ===\n"
        )
        sys.stderr.flush()
        source_path: Path | None = None
        if prebuild is not None:
            source_path = source_dir / f"w2_native_{n_rows}.parquet"
            sys.stderr.write(f"  prebuilding source ({args.prebuild}) -> {source_path} ...\n")
            sys.stderr.flush()
            build_cmd = [str(VENV_PY), str(prebuild), str(n_rows), str(source_path)]
            if args.batch_rows is not None:
                build_cmd.append(str(args.batch_rows))
            subprocess.run(  # noqa: S603 fixed local benchmark command, no untrusted input
                build_cmd, cwd=str(ENGINE), env=_WORKER_ENV, check=True
            )
        worker_args = [str(source_path)] if source_path is not None else []
        if args.batch_rows is not None:
            worker_args.append(str(args.batch_rows))
        for w in range(args.warmup):
            sys.stderr.write(f"  warmup {w + 1}/{args.warmup} ...\n")
            sys.stderr.flush()
            wr = run_rep(n_rows, worker, worker_args)
            sys.stderr.write(f"    warmup wall={wr['wall_s']:.2f}s rss={wr['peak_rss_kb']}kb\n")
            sys.stderr.flush()
        reps = []
        for i in range(args.reps):
            r = run_rep(n_rows, worker, worker_args)
            reps.append(r)
            sys.stderr.write(
                f"  rep {i + 1}/{args.reps}: wall={r['wall_s']:.2f}s "
                f"rss={r['peak_rss_kb']}kb mode={r.get('execution_mode')}\n"
            )
            sys.stderr.flush()
        summ = summarize(n_rows, reps)
        all_results[str(n_rows)] = summ
        sys.stderr.write(
            f"  SUMMARY n={n_rows}: median={summ['wall_median_s']:.2f}s "
            f"IQR={summ['wall_iqr_s']:.2f}s p95={summ['wall_p95of_s']:.2f}s "
            f"rss_max={summ['peak_rss_max_mb']}MB "
            f"hash_tput={summ['hash_tput_median_rows_s']:.0f}rows/s\n"
        )
        sys.stderr.flush()
        if source_path is not None:
            source_path.unlink(missing_ok=True)
        # Persist incrementally so a later-tier OOM doesn't lose earlier tiers.
        Path(args.out).write_text(json.dumps(all_results, indent=2))

    Path(args.out).write_text(json.dumps(all_results, indent=2))
    print(json.dumps(all_results, indent=2))


if __name__ == "__main__":
    main()
