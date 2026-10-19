"""Bounded-generation RSS bench under an external supervisor (Phase F FR0).

Plan: docs/plans/2026-10-18-phase-f-rebaseline-f1-f3.md sections 3 and 6. Streams a two-table
generation plan (a parent with a sequence key, a categorical, a pooled Faker column and a nullable
column; a child that references the parent key and carries its own pooled Faker column) into a
`ParquetTransactionalSink` through `generate_tables_streamed`, under one byte budget and one frozen
batch size. The supervisor (`_supervisor.run_worker`) measures peak RSS from OUTSIDE the worker.
That figure is a bench-only measurement and containment; the memory guarantee itself is the
ledger's reserve-before-allocate refusal, tested in-process.

Prints one `BENCH_JSON {...}` line. `completed` is true only when the run streamed (mode
`streamed`), the sink committed, and the committed row counts equal the request.

    python scripts/fk-scale/bench_worker_gen.py --rows 5000000 --budget-mb 1024 --rss-ceiling-mb 1500

`--plan rep` (Phase F F4a) swaps in the representative plan from `_f4a_gen` (sequence, categorical,
null_mask, pooled faker, reference, windowed_date), adds an output digest and rows/s, and
`--mode probe` instead runs that plan resident under the F0 witness to report each column's executed
backend. The default `--plan fr0` keeps the FR0 bounded-RSS test's plan unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _f4a_gen
from _supervisor import classify_failure, run_worker

SCHEMA = "fr0-gen-bounded/v1"
F4A_SCHEMA = "f4a-gen-capacity/v1"
PARENT_ROWS = 100_000  # fixed across tiers so the parent key pool does not scale with the tier
BATCH_ROWS = 32_768


def _status_mb(field: str) -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(field + ":"):
                return round(int(line.split()[1]) / 1024.0, 1)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _error_record(exc: BaseException) -> dict[str, Any]:
    # Class, coded error and call-site frames only: a message can carry a data value.
    frames = [
        f"{Path(f.filename).name}:{f.lineno}:{f.name}"
        for f in traceback.extract_tb(exc.__traceback__)
    ]
    return {"type": type(exc).__name__, "code": getattr(exc, "code", None), "frames": frames}


def build_config(rows: int, plan: str = "fr0") -> dict[str, Any]:
    if plan == "rep":
        return _f4a_gen.representative_config(rows, PARENT_ROWS)
    return {
        "version": 1,
        "global_settings": {"seed": 20261018},
        "sources": {},
        "targets": {},
        "tables": [
            {
                "name": "orders",
                "row_count": rows,
                "generate_columns": [
                    {"name": "oid", "type": "sequence", "start": 1, "prefix": "O"},
                    {
                        "name": "pid",
                        "type": "reference",
                        "reference_table": "people",
                        "reference_column": "id",
                        "distribution": "random",
                    },
                    {"name": "status", "type": "categorical", "categories": ["a", "b", "c"]},
                    {"name": "ship_city", "type": "faker", "faker_type": "city"},
                ],
            },
            {
                "name": "people",
                "row_count": PARENT_ROWS,
                "generate_columns": [
                    {"name": "id", "type": "sequence", "start": 1, "prefix": "P", "pad_length": 8},
                    {
                        "name": "tier",
                        "type": "categorical",
                        "categories": ["x", "y"],
                        "weights": [3, 1],
                        "null_probability": 0.1,
                    },
                    {"name": "city", "type": "faker", "faker_type": "city"},
                ],
            },
        ],
    }


def _run(args: argparse.Namespace, work: Path) -> dict[str, Any]:
    from datetime import datetime, timezone

    import pyarrow.parquet as pq

    from decoy_engine.execution import ParquetTransactionalSink
    from decoy_engine.generation._plan_entry import generate_tables_streamed
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import Profile

    profile = Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        decoy_engine_version="fr0-bench",
    )
    plan = compile_plan(
        build_config(args.rows, args.plan), profile, decoy_engine_version="fr0-bench"
    )
    committed = work / "committed"
    rss_before = _status_mb("VmRSS")
    t0 = time.perf_counter()
    result = generate_tables_streamed(
        plan,
        sink=ParquetTransactionalSink(committed),
        budget_bytes=args.budget_mb * 1024 * 1024,
        batch_rows=BATCH_ROWS,
    )
    gen_s = time.perf_counter() - t0
    rows = {p.stem: pq.ParquetFile(p).metadata.num_rows for p in committed.glob("*.parquet")}
    expected = {"orders": args.rows, "people": PARENT_ROWS}
    total_rows = sum(rows.values())
    return {
        "completed_inner": result.mode == "streamed" and rows == expected,
        "rows_per_s": round(total_rows / gen_s) if gen_s else None,
        "mode": result.mode,
        "reason": result.reason,
        "committed_rows": rows,
        "expected_rows": expected,
        "reported_rows": result.row_counts,
        "peak_reserved_mb": round(result.peak_reserved_bytes / (1024 * 1024), 1),
        "gen_s": round(gen_s, 2),
        "rss_before_mb": rss_before,
        "rss_hwm_after_mb": _status_mb("VmHWM"),
    }


def run_child(args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parents[2]
    # This checkout's engine wins over another worktree's editable install.
    sys.path[:0] = [str(repo / "src"), str(repo)]
    result_path = Path(args.result_path)
    record: dict[str, Any] = {"completed_inner": False}
    try:
        if args.mode == "probe":
            cfg = (
                _f4a_gen.mixed_parent_config(args.rows)
                if args.plan == "mixed-parent"
                else build_config(args.rows, "rep")
            )
            probe = _f4a_gen.probe_backends(cfg)
            record.update(probe, completed_inner=bool(probe["core_eligible"]))
        else:
            record.update(_run(args, Path(args.work_dir)))
    except BaseException as exc:
        record["error"] = _error_record(exc)
    tmp = result_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(result_path)  # atomic: the supervisor never sees a half-written file
    return 0 if record["completed_inner"] else 3


def supervise(args: argparse.Namespace) -> dict[str, Any]:
    work = Path(tempfile.mkdtemp(prefix="fr0-gen-", dir=args.work_root))
    scratch = work / "scratch"
    scratch.mkdir()
    env = {**os.environ, "TMPDIR": str(scratch), "ARROW_DEFAULT_MEMORY_POOL": "system"}
    result_path = work / "result.json"
    cmd = [
        sys.executable, str(Path(__file__).resolve()), "--child",
        "--work-dir", str(work), "--result-path", str(result_path),
        "--rows", str(args.rows), "--budget-mb", str(args.budget_mb),
        "--plan", args.plan, "--mode", args.mode,
    ]  # fmt: skip
    try:
        outcome = run_worker(
            cmd,
            env=env,
            result_path=result_path,
            timeout_s=args.timeout_s,
            rss_kill_mb=args.rss_kill_mb,
        )
        failure = classify_failure(outcome)
        # Digested after the measured process exited; see `digest_in_subprocess`.
        digest = (
            _f4a_gen.digest_in_subprocess(work / "committed")
            if failure is None and args.plan == "rep" and args.mode == "capacity"
            else None
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {
        "schema": F4A_SCHEMA if args.plan == "rep" else SCHEMA,
        "mode": args.mode,
        "completed": failure is None,
        "failure": failure,
        "requested": {
            "rows": args.rows,
            "parent_rows": PARENT_ROWS,
            "budget_mb": args.budget_mb,
            "batch_rows": BATCH_ROWS,
            "rss_ceiling_mb": args.rss_ceiling_mb,
            "plan": args.plan,
        },
        "peak_rss_mb": outcome.peak_rss_mb,
        "peak_rss_poll_mb": outcome.peak_rss_poll_mb,
        "rss_method": "wait4_ru_maxrss",
        "rss_within_ceiling": (
            (failure is None and outcome.peak_rss_mb <= args.rss_ceiling_mb)
            if args.rss_ceiling_mb
            else None
        ),
        "wall_s": round(outcome.wall_s, 2),
        "digest": digest,
        "result": outcome.result or {},
    }


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--rows", type=int, required=True, help="rows in the child table")
    ap.add_argument("--budget-mb", type=int, default=1024)
    ap.add_argument("--plan", choices=("fr0", "rep", "mixed-parent"), default="fr0")
    ap.add_argument("--mode", choices=("capacity", "probe"), default="capacity")
    ap.add_argument(
        "--rss-ceiling-mb", type=float, default=None, help="declared acceptance ceiling"
    )
    ap.add_argument("--rss-kill-mb", type=float, default=None, help="supervisor safety kill")
    ap.add_argument("--timeout-s", type=float, default=None)
    ap.add_argument("--work-root", default=None)
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
    return 0 if record["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
