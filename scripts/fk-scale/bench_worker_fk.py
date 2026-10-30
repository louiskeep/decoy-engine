"""FK `run_pipeline` out-of-core bench worker with an external supervisor (D4).

Plan: docs/plans/2026-10-15-d4-scale-proof.md section 3.1 and 4. Runs `run_pipeline`
(AUTO route by default; `--force-ooc` forces `out_of_core`) over a disk-backed FK chain
written by `build_fk_chain.py`, and prints one `BENCH_JSON {...}` line.

Two processes. The SUPERVISOR (default mode, imports no engine) spawns the worker in a
fresh subprocess per repeat, measures peak RSS from outside, and always emits a record,
even when the worker is OOM-killed, signalled, hung, or leaves no usable result. The
WORKER (`--child`) runs the pipeline and writes its own result file. `completed` is true
only when the run returned, committed row counts equal the input counts, and bounded FK
parity passed on every edge; rc=0 alone is never success.

Three distinct limits, never conflated (plan section 4):
  --budget-mb     the engine OOC memory BUDGET passed to `run_pipeline` (resolved bytes)
  --rss-ceiling-mb  the declared acceptance CEILING; compared with the EXTERNAL peak RSS
  --mem-cap-mb    an optional RLIMIT_DATA hard cap on the worker, for forced-OOM checks
                  (NOT RSS and not RLIMIT_AS)

    python scripts/fk-scale/bench_worker_fk.py --data-dir /data/fk10 --budget-mb 12000 \\
        --rss-ceiling-mb 6656 --reorder on
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _supervisor import classify_failure, run_worker

SCHEMA = "fk-scale/v1"
_MIB = 1024 * 1024
TABLES = ("parent", "child", "grandchild")


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered)) - 1))]


# ----------------------------------------------------------------------------- worker


class _DiskPeakSampler:
    """Polls bytes under `root` and keeps the max.

    Same method and caveat as `scripts/fk_memory_probe.py`'s sampler: the runner deletes
    its staging on success, so the high-water mark exists only DURING the run, and a
    burst shorter than the poll interval can be missed.
    """

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


def _payload_columns(schema_names: list[str], keys: set[str]) -> list[str]:
    return [c for c in schema_names if c not in keys]


def _column(name: str, strategy: str, namespace: str) -> dict[str, Any]:
    col: dict[str, Any] = {"name": name, "strategy": strategy, "namespace": namespace}
    if strategy == "fpe":
        col["provider_config"] = {"charset": "alphanum"}
    return col


def _build_config(paths: dict[str, Path], out_dir: Path, strategy: str) -> dict[str, Any]:
    import pyarrow.parquet as pq

    key_cols = {"parent": {"id"}, "child": {"id", "parent_id"}, "grandchild": {"child_id"}}
    fk_ns = {
        "parent": {"id": "fk_ns_parent"},
        "child": {"id": "fk_ns_child", "parent_id": "fk_ns_parent"},
        "grandchild": {"child_id": "fk_ns_child"},
    }
    tables = []
    for t in TABLES:
        names = pq.ParquetFile(paths[t]).schema_arrow.names
        cols = [_column(c, "hash", ns) for c, ns in fk_ns[t].items()]
        cols += [_column(c, strategy, f"payload_{t}") for c in _payload_columns(names, key_cols[t])]
        tables.append({"name": t, "columns": cols})
    rel = lambda p, pc, c, cc, ns: {
        "parent": {"table": p, "columns": [pc]},
        "children": [{"table": c, "columns": [cc]}],
        "orphan_policy": "preserve",
        "namespace": ns,
    }
    return {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            t: {"type": "file", "path": str(paths[t]), "format": "parquet"} for t in TABLES
        },
        "targets": {
            t: {"type": "file", "format": "parquet", "path": str(out_dir / f"{t}.out.parquet")}
            for t in TABLES
        },
        "tables": tables,
        "relationships": [
            rel("parent", "id", "child", "parent_id", "fk_ns_parent"),
            rel("child", "id", "grandchild", "child_id", "fk_ns_child"),
        ],
    }


def _disposition(quality_metrics: dict[str, Any]) -> dict[str, Any]:
    """Which columns ran the compiled kernel and which ran Python.

    A successful companion import proves nothing about execution; the engine stamps a
    per-column `executed_backend` and that is the evidence (plan section 1). Keys are
    discovered, not enumerated, so a new native operator's entries are not missed.
    """
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
                "path": e.get("path"),
                "executed_backend": e["executed_backend"],
            }
            for e in entries
            if e["executed_backend"] != RUST_COMPANION
        ],
    }


def _resolve_threshold(reorder: str, counts: dict[str, int]) -> int | None:
    """`on` = the engine default; `off` = above the largest parent cardinality, which
    forces the batch/DuckDB joiner (plan section 4); an integer is an explicit override."""
    if reorder == "on":
        return None
    if reorder == "off":
        return max(counts.values()) + 1
    return int(reorder)


def _apply_mem_cap(cap_mb: int) -> None:
    import resource

    cap = cap_mb * _MIB
    resource.setrlimit(resource.RLIMIT_DATA, (cap, cap))


def _error_record(exc: BaseException) -> dict[str, Any]:
    # Class, coded error, and call-site frames only: a message or a frame's locals/source
    # line can carry a data value (log hygiene). Frames are file:line:function, so a future
    # failure reports where it threw without leaking payload.
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
    # This checkout's engine and test fixtures win over any editable install of another
    # worktree (a venv's .pth can point elsewhere), so the measured code is the code here.
    repo = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repo / "src"), str(repo)]
    from _parity import table_rows, verify_committed_chain
    from tests.perf_fixtures.ooc_route_witness import witness_ooc_route

    from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
    from decoy_engine.keyprovider import SecretKeyProvider
    from decoy_engine.profile._readers import LazySource

    data_dir, work = Path(args.data_dir), Path(args.work_dir)
    paths = {t: data_dir / f"{t}.parquet" for t in TABLES}
    counts = {t: table_rows(p) for t, p in paths.items()}
    committed = work / "committed"
    config = _build_config(paths, work / "cfg-targets", args.strategy)
    threshold = _resolve_threshold(args.reorder, counts)
    budget = args.budget_mb * _MIB if args.budget_mb else None
    scratch = Path(tempfile.gettempdir())  # TMPDIR was pointed at this run's scratch dir

    sampler = _DiskPeakSampler(scratch)
    sampler.start()
    t0 = time.perf_counter()
    try:
        with witness_ooc_route() as witness:
            result = run_pipeline(
                config,
                {t: LazySource(path=p) for t, p in paths.items()},
                engine_version="d4-scale-proof",
                # Keyed strategies need a secret in every release phase; this bench measures memory.
                key_provider=SecretKeyProvider(b"decoy-bench-throwaway-secret-0123456789"),
                execution_mode="out_of_core" if args.force_ooc else "auto",
                sink=ParquetTransactionalSink(committed),
                out_of_core_budget_bytes=budget,
                out_of_core_reorder_threshold_rows=threshold,
                native_threads=args.native_threads,
            )
    finally:
        mask_s = time.perf_counter() - t0
        temp_disk_peak_mb = sampler.stop()

    execution = result.quality_metrics.get("execution", {})
    verdict = verify_committed_chain(paths, committed)
    w = witness.as_dict()
    return {
        "completed_inner": bool(verdict["rowcount_match"] and verdict["parity"]["status"] == "ok"),
        "mask_s": round(mask_s, 2),
        "rows": counts,
        "route": {
            "execution_mode": execution.get("execution_mode"),
            "route_reason": execution.get("route_reason"),
            "forced": bool(args.force_ooc),
        },
        "reorder": {
            "taken": w["reorder_taken"],
            "driver_by_table": w["driver_by_table"],
            "threshold_rows": threshold,
            "setting": args.reorder,
        },
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


def _child_cmd(args: argparse.Namespace, work: Path, result_path: Path) -> list[str]:
    cmd = [
        sys.executable, str(Path(__file__).resolve()), "--child",
        "--data-dir", args.data_dir, "--work-dir", str(work), "--result-path", str(result_path),
        "--strategy", args.strategy, "--reorder", args.reorder, "--native-threads", str(args.native_threads),
    ]  # fmt: skip
    if args.force_ooc:
        cmd.append("--force-ooc")
    if args.budget_mb:
        cmd += ["--budget-mb", str(args.budget_mb)]
    if args.mem_cap_mb:
        cmd += ["--mem-cap-mb", str(args.mem_cap_mb)]
    return cmd


def _one_run(args: argparse.Namespace, run_no: int) -> dict[str, Any]:
    work = Path(tempfile.mkdtemp(prefix=f"fk-scale-run{run_no}-", dir=args.work_root))
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
    peak = max(r["peak_rss_mb"] for r in runs)
    first_failure = next((r["failure"] for r in runs if r["failure"]), None)
    record = {
        "schema": SCHEMA,
        "completed": all(r["completed"] for r in runs),
        "failure": first_failure,
        "requested": {
            "execution_mode": "out_of_core" if args.force_ooc else "auto",
            "route_forced": bool(args.force_ooc),
            "reorder": args.reorder,
            "strategy": args.strategy,
            "budget_mb": args.budget_mb,
            "rss_ceiling_mb": args.rss_ceiling_mb,
            "mem_cap_mb": args.mem_cap_mb,
            "mem_cap_kind": "RLIMIT_DATA" if args.mem_cap_mb else None,
            "native_threads": args.native_threads,
            "cpu_count": os.cpu_count(),
            "timeout_s": args.timeout_s,
            "repeats": args.repeats,
        },
        "rows": last.get("rows"),
        "total_rows": sum(last["rows"].values()) if last.get("rows") else None,
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
        "peak_rss_mb": peak,
        "peak_rss_poll_mb": max(r["peak_rss_poll_mb"] for r in runs),
        "rss_method": "wait4_ru_maxrss",
        "rss_within_ceiling": (peak <= args.rss_ceiling_mb) if args.rss_ceiling_mb else None,
        "runs": [{k: v for k, v in r.items() if k != "result"} for r in runs],
    }
    return record


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--data-dir", required=True, help="dir holding parent/child/grandchild.parquet")
    ap.add_argument(
        "--force-ooc", action="store_true", help="force execution_mode=out_of_core (default: AUTO)"
    )
    ap.add_argument(
        "--reorder",
        default="on",
        help="on (engine default threshold) | off (batch joiner) | <rows> explicit threshold",
    )
    ap.add_argument(
        "--strategy",
        choices=("hash", "fpe"),
        default="hash",
        help="payload column strategy; FK keys are always hash",
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
    line = json.dumps(record)
    print(f"BENCH_JSON {line}")
    if args.out:
        Path(args.out).write_text(line + "\n")
    return 0 if record["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
