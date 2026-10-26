"""Synthea `run_pipeline` scale-characterization worker with an external supervisor.

Masks the Synthea-shaped FK chain written by `build_synthea_dataset.py` through the
normal `run_pipeline` path with the native companion ON, and prints one
`BENCH_JSON {...}` line holding an honest size-to-time record: wall time,
throughput (rows/s), per-table rows, table + column counts, externally-measured
peak RSS, temp-disk spill, the chosen route, the per-column native disposition,
FK parity, and host provenance (engine version, substrate, machine, run date).

It measures SPEED, MEMORY and THROUGHPUT only. V1 makes no distribution-fidelity
or differential-privacy claim, so this harness asserts none.

Two processes, the same pattern as `scripts/fk-scale/bench_worker_fk.py` (whose
`_supervisor` this reuses rather than forks): the SUPERVISOR spawns the worker in
a fresh subprocess per repeat, measures peak RSS from OUTSIDE (so an OOM-killed or
hung worker still yields a record), and `completed` is true only when the run
returned, committed row counts equal the inputs, and bounded FK parity passed.

Three distinct limits, never conflated:
  --budget-mb       engine out-of-core memory BUDGET passed to run_pipeline
  --rss-ceiling-mb  declared acceptance CEILING, compared with the EXTERNAL peak RSS
  --mem-cap-mb      optional RLIMIT_DATA hard cap on the worker (forced-OOM checks)

    python scripts/synthea-scale/bench_worker_synthea.py --data-dir /data/synthea10m \\
        --rss-ceiling-mb 6656 --out results/synthea-10m.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "fk-scale"))  # reuse the D4 supervisor, do not fork it

from _supervisor import classify_failure, run_worker  # noqa: E402  (after sys.path setup)

SCHEMA = "synthea-scale/v1"
_MIB = 1024 * 1024


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered)) - 1))]


# ----------------------------------------------------------------------------- worker


class _DiskPeakSampler:
    """Polls bytes under `root` and keeps the max (same method as the probe's sampler:
    the runner deletes staging on success, so the high-water mark exists only DURING
    the run, and a burst shorter than the poll interval can be missed)."""

    def __init__(self, root: Path, interval_s: float = 0.2) -> None:
        import threading

        self._root, self._interval = root, interval_s
        self._stop = threading.Event()
        self._peak = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> float:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._sample()
        return self._peak / _MIB

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self._interval)

    def _sample(self) -> None:
        total = 0
        for dirpath, _dirs, files in os.walk(self._root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        self._peak = max(self._peak, total)


def _native_companion() -> dict[str, Any]:
    """Whether the Rust companion is importable; the run is 'native on' only if so."""
    try:
        import decoy_engine_native._kernel as kernel

        return {"present": True, "kernel": getattr(kernel, "__file__", None)}
    except Exception as exc:  # pragma: no cover - reported, not raised
        return {"present": False, "error": type(exc).__name__}


def _disposition(quality_metrics: dict[str, Any]) -> dict[str, Any]:
    """Per-column executed backend (native kernel vs Python). A successful companion
    import proves nothing about execution; the engine stamps `executed_backend` and
    that is the evidence. Keys are discovered, not enumerated (same as the FK worker)."""
    from decoy_engine.execution._operator_registry import RUST_COMPANION

    entries = [
        e
        for v in quality_metrics.values()
        if isinstance(v, list)
        for e in v
        if isinstance(e, dict) and "executed_backend" in e and "column" in e
    ]
    by_backend: dict[str, int] = {}
    for e in entries:
        by_backend[str(e["executed_backend"])] = by_backend.get(str(e["executed_backend"]), 0) + 1
    return {
        "columns_total": len(entries),
        "native_columns": sum(1 for e in entries if e["executed_backend"] == RUST_COMPANION),
        "by_backend": by_backend,
        "non_native": [
            {
                "table": e.get("table"),
                "column": e["column"],
                "executed_backend": e["executed_backend"],
            }
            for e in entries
            if e["executed_backend"] != RUST_COMPANION
        ],
    }


def _apply_mem_cap(cap_mb: int) -> None:
    import resource

    cap = cap_mb * _MIB
    resource.setrlimit(resource.RLIMIT_DATA, (cap, cap))


def _error_record(exc: BaseException) -> dict[str, Any]:
    # Class, coded error, call-site frames only (file:line:function): a message or a
    # frame's locals could carry a data value (log hygiene). Same shape as the FK worker.
    frames = [
        f"{Path(f.filename).name}:{f.lineno}:{f.name}"
        for f in traceback.extract_tb(exc.__traceback__)
    ]
    return {"type": type(exc).__name__, "code": getattr(exc, "code", None), "frames": frames}


def run_child(args: argparse.Namespace) -> int:
    result_path = Path(args.result_path)
    record: dict[str, Any] = {"completed_inner": False}
    try:
        if args.mem_cap_mb:
            _apply_mem_cap(args.mem_cap_mb)
        record.update(_run_pipeline_once(args))
    except BaseException as exc:
        record["error"] = _error_record(exc)
    tmp = result_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(result_path)  # atomic: the supervisor never sees a half-written file
    return 0 if record["completed_inner"] else 3


def _run_pipeline_once(args: argparse.Namespace) -> dict[str, Any]:
    # This checkout wins over any editable install elsewhere, so measured code == this code.
    repo = _HERE.parents[1]
    sys.path[:0] = [str(repo / "src"), str(repo)]
    from _synthea_config import build_config
    from _synthea_layout import TABLE_NAMES, total_columns
    from _synthea_parity import table_rows, verify_committed_chain
    from tests.perf_fixtures.ooc_route_witness import witness_ooc_route

    import decoy_engine
    from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
    from decoy_engine.profile._readers import LazySource

    companion = _native_companion()
    if not companion["present"] and not args.allow_no_native:
        raise RuntimeError(
            "native companion not importable; pass --allow-no-native to measure pandas-only"
        )

    data_dir, work = Path(args.data_dir), Path(args.work_dir)
    paths = {t: data_dir / f"{t}.parquet" for t in TABLE_NAMES}
    counts = {t: table_rows(p) for t, p in paths.items()}
    committed = work / "committed"
    config = build_config(paths, work / "cfg-targets")
    budget = args.budget_mb * _MIB if args.budget_mb else None
    scratch = Path(tempfile.gettempdir())  # TMPDIR points at this run's scratch dir

    sampler = _DiskPeakSampler(scratch)
    sampler.start()
    t0 = time.perf_counter()
    try:
        with witness_ooc_route() as witness:
            result = run_pipeline(
                config,
                {t: LazySource(path=p) for t, p in paths.items()},
                engine_version=decoy_engine.__version__,
                execution_mode="out_of_core" if args.force_ooc else "auto",
                sink=ParquetTransactionalSink(committed),
                out_of_core_budget_bytes=budget,
                native_threads=args.native_threads,
            )
    finally:
        mask_s = time.perf_counter() - t0
        temp_disk_peak_mb = sampler.stop()

    execution = result.quality_metrics.get("execution", {})
    verdict = verify_committed_chain(paths, committed)
    w = witness.as_dict()
    total_rows = sum(counts.values())
    return {
        "completed_inner": bool(verdict["rowcount_match"] and verdict["parity"]["status"] == "ok"),
        "mask_s": round(mask_s, 2),
        "throughput_rows_per_s": round(total_rows / mask_s) if mask_s > 0 else None,
        "rows": counts,
        "total_rows": total_rows,
        "table_count": len(TABLE_NAMES),
        "column_count": total_columns(),
        "native_companion": companion,
        "route": {
            "execution_mode": execution.get("execution_mode"),
            "route_reason": execution.get("route_reason"),
            "forced": bool(args.force_ooc),
        },
        "reorder": {"taken": w["reorder_taken"], "driver_by_table": w["driver_by_table"]},
        "sizing": w["runner_args"],
        "spill": {**w["spill"], "temp_disk_peak_mb": round(temp_disk_peak_mb, 1)},
        "disposition": _disposition(result.quality_metrics),
        "rowcount_match": verdict["rowcount_match"],
        "rows_out": verdict["rows_out"],
        "parity": verdict["parity"],
        "committed_output_mb": round(_dir_mb(committed), 1),
    }


def _dir_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / _MIB


# -------------------------------------------------------------------------- supervisor


def _machine() -> dict[str, Any]:
    import psutil

    return {
        "node": platform.node(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "total_ram_mb": round(psutil.virtual_memory().total / _MIB),
        "python": platform.python_version(),
    }


def _child_cmd(args: argparse.Namespace, work: Path, result_path: Path) -> list[str]:
    cmd = [
        sys.executable, str(Path(__file__).resolve()), "--child",
        "--data-dir", args.data_dir, "--work-dir", str(work), "--result-path", str(result_path),
        "--native-threads", str(args.native_threads),
    ]  # fmt: skip
    if args.force_ooc:
        cmd.append("--force-ooc")
    if args.allow_no_native:
        cmd.append("--allow-no-native")
    if args.budget_mb:
        cmd += ["--budget-mb", str(args.budget_mb)]
    if args.mem_cap_mb:
        cmd += ["--mem-cap-mb", str(args.mem_cap_mb)]
    return cmd


def _one_run(args: argparse.Namespace, run_no: int) -> dict[str, Any]:
    work = Path(tempfile.mkdtemp(prefix=f"synthea-run{run_no}-", dir=args.work_root))
    scratch = work / "scratch"
    scratch.mkdir()
    env = {**os.environ, "TMPDIR": str(scratch), "ARROW_DEFAULT_MEMORY_POOL": "system"}
    try:
        outcome = run_worker(
            _child_cmd(args, work, work / "result.json"),
            env=env,
            result_path=work / "result.json",
            timeout_s=args.timeout_s,
            rss_kill_mb=args.rss_kill_mb,
        )
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    failure = classify_failure(outcome)
    return {
        "completed": failure is None,
        "failure": failure,
        "wall_s": round(outcome.wall_s, 2),
        "peak_rss_mb": outcome.peak_rss_mb,
        "peak_rss_poll_mb": outcome.peak_rss_poll_mb,
        "returncode": outcome.returncode,
        "signal": outcome.term_signal,
        "result": outcome.result or {},
    }


def supervise(args: argparse.Namespace) -> dict[str, Any]:
    runs = [_one_run(args, i) for i in range(args.repeats)]
    last = runs[-1]["result"]
    mask = [r["result"]["mask_s"] for r in runs if "mask_s" in r["result"]]
    thr = [
        r["result"]["throughput_rows_per_s"]
        for r in runs
        if r["result"].get("throughput_rows_per_s")
    ]
    peak = max(r["peak_rss_mb"] for r in runs)
    first_failure = next((r["failure"] for r in runs if r["failure"]), None)
    return {
        "schema": SCHEMA,
        "completed": all(r["completed"] for r in runs),
        "failure": first_failure,
        "run_date_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": _machine(),
        "requested": {
            "execution_mode": "out_of_core" if args.force_ooc else "auto",
            "route_forced": bool(args.force_ooc),
            "budget_mb": args.budget_mb,
            "rss_ceiling_mb": args.rss_ceiling_mb,
            "mem_cap_mb": args.mem_cap_mb,
            "mem_cap_kind": "RLIMIT_DATA" if args.mem_cap_mb else None,
            "native_threads": args.native_threads,
            "timeout_s": args.timeout_s,
            "repeats": args.repeats,
        },
        "rows": last.get("rows"),
        "total_rows": last.get("total_rows"),
        "table_count": last.get("table_count"),
        "column_count": last.get("column_count"),
        "native_companion": last.get("native_companion"),
        "route": last.get("route"),
        "reorder": last.get("reorder"),
        "sizing": last.get("sizing"),
        "spill": last.get("spill"),
        "disposition": last.get("disposition"),
        "rowcount_match": last.get("rowcount_match"),
        "parity": last.get("parity"),
        "committed_output_mb": last.get("committed_output_mb"),
        "mask_s": mask[-1] if mask else None,
        "mask_s_p50": round(statistics.median(mask), 2) if mask else None,
        "mask_s_p95": round(_percentile(mask, 95), 2) if mask else None,
        "mask_s_runs": mask,
        "throughput_rows_per_s": thr[-1] if thr else None,
        "peak_rss_mb": peak,
        "peak_rss_poll_mb": max(r["peak_rss_poll_mb"] for r in runs),
        "rss_method": "wait4_ru_maxrss",
        "rss_within_ceiling": (peak <= args.rss_ceiling_mb) if args.rss_ceiling_mb else None,
        "runs": [{k: v for k, v in r.items() if k != "result"} for r in runs],
    }


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument(
        "--data-dir", required=True, help="dir holding patients/encounters/observations.parquet"
    )
    ap.add_argument(
        "--force-ooc", action="store_true", help="force execution_mode=out_of_core (default: AUTO)"
    )
    ap.add_argument(
        "--budget-mb",
        type=int,
        default=None,
        help="engine OOC memory budget (MiB); default: host detection",
    )
    ap.add_argument(
        "--rss-ceiling-mb",
        type=float,
        default=None,
        help="declared acceptance ceiling for external peak RSS",
    )
    ap.add_argument(
        "--rss-kill-mb",
        type=float,
        default=None,
        help="supervisor safety kill when polled RSS exceeds this",
    )
    ap.add_argument(
        "--mem-cap-mb",
        type=int,
        default=None,
        help="RLIMIT_DATA cap on the worker (forced-OOM checks)",
    )
    ap.add_argument(
        "--allow-no-native",
        action="store_true",
        help="permit a pandas-only run when the companion is absent",
    )
    ap.add_argument("--native-threads", type=int, default=1)
    ap.add_argument("--timeout-s", type=float, default=None)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument(
        "--work-root", default=None, help="parent dir for per-run scratch + committed output"
    )
    ap.add_argument("--keep", action="store_true", help="keep per-run work dirs")
    ap.add_argument("--out", default=None, help="also write the record to this file")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--work-dir", help=argparse.SUPPRESS)
    ap.add_argument("--result-path", help=argparse.SUPPRESS)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.child:
        return run_child(args)
    record = supervise(args)
    print(f"BENCH_JSON {json.dumps(record)}")
    if args.out:
        Path(args.out).write_text(json.dumps(record, indent=2) + "\n")
    return 0 if record["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
