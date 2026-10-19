"""Mixed (generate + mask) `run_pipeline` capacity bench with an external supervisor (F4).

Plan: docs/plans/2026-10-17-phase-f-f4-capacity.md. Drives the PRODUCTION `run_pipeline`
through the shipped F2 mixed route: a GENERATE-kind parent (`people`, resident) plus a
masked child (`accounts`) over one FK edge, on the bounded chunked lane, published
through a `ParquetTransactionalSink` so child output is streamed, not held. Prints one
`BENCH_JSON {...}` line.

Two modes, always separate processes (the oracle's whole-output materialization would
dominate RSS, so it is never alongside a capacity-measured run):

  --mode capacity  supervised production run(s); the record carries wall `mask_s`
                   p50/p95, EXTERNAL peak RSS (`wait4` ru_maxrss) and the distinct
                   diagnostics below.
  --mode oracle    own supervised process: runs production to a sink, then the
                   `full_frame` oracle, and compares canonical Arrow schema plus value
                   bytes (F2's `assert_outputs_identical`). Its RSS is reported but is
                   NOT a capacity figure.

Capacity diagnostics, kept as separate fields (ru_maxrss is whole-process and cannot
attribute bytes to the parent; these are observations, not a breakdown):
  generation phase    RSS / HWM right after `generate_tables`, plus parent Arrow nbytes
  carrier phase       resident child-key + resolved-key arrays and RSS after the resolve
  external peak       supervisor ru_maxrss over the worker's whole life
  temp disk           peak bytes under the run's scratch dir and staged output dir

`completed` is true only when the run returned, route evidence (mode `mixed`, streamed
outputs, empty `result.outputs`, chunked lane, native hash columns when the companion is
present) held, committed row counts equal the input, and FK/payload parity passed.

    python scripts/fk-scale/bench_worker_mixed.py --parent-rows 200000 --child-rows 800000 \\
        --rss-ceiling-mb 8192
"""

from __future__ import annotations

import argparse
import importlib
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

import _f4a_gen
from _supervisor import classify_failure, run_worker
from bench_worker_fk import _DiskPeakSampler, _percentile

SCHEMA = "f4-mixed/v1"
ORACLE_SCHEMA = "f4-mixed-oracle/v1"
_MIB = 1024 * 1024
DEFAULT_ORPHAN_EVERY = 100  # 1% orphan child rows
DEFAULT_WIDTH = 3  # hashed payload columns beside email (modest, to control memory)


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


class _MixedProbe:
    """Wraps the route's generate and carrier steps to read RSS and resident bytes.

    Observation only: each wrapper calls the original and returns its value unchanged."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def install(self) -> None:
        route: Any = importlib.import_module("decoy_engine.execution._pipeline_mixed_route")

        generate, build_carrier = route._generate, route._build_carrier

        def probed_generate(ctx: Any) -> Any:
            t0 = time.perf_counter()
            tables = generate(ctx)
            self.data["generation"] = {
                "gen_s": round(time.perf_counter() - t0, 2),
                "rss_after_mb": _status_mb("VmRSS"),
                "hwm_after_mb": _status_mb("VmHWM"),
                "parent_arrow_nbytes": {t: int(v.nbytes) for t, v in tables.items()},
                "parent_arrow_mb": round(sum(v.nbytes for v in tables.values()) / _MIB, 1),
            }
            return tables

        def probed_carrier(static: Any, sources: Any, generate_outputs: Any) -> Any:
            t0 = time.perf_counter()
            carrier, warnings = build_carrier(static, sources, generate_outputs)
            held = 0 if carrier is None else carrier.source.nbytes + carrier.resolved.nbytes
            self.data["carrier"] = {
                "carrier_s": round(time.perf_counter() - t0, 2),
                "resident_key_mb": round(held / _MIB, 1),
                "rss_after_mb": _status_mb("VmRSS"),
                "hwm_after_mb": _status_mb("VmHWM"),
                "fk_warnings": len(warnings),
            }
            return carrier, warnings

        route._generate = probed_generate
        route._build_carrier = probed_carrier


def _route_evidence(result: Any, expect_chunks: bool, companion: bool) -> dict[str, Any]:
    """F2's own route signals; `ok` is the conjunction the worker requires."""
    from _mixed_layout import disposition

    qm = result.quality_metrics
    execution = qm.get("execution", {})
    disp = disposition(qm)
    streamed = bool(execution.get("outputs_streamed"))
    checks = {
        "execution_mode_mixed": execution.get("execution_mode") == "mixed",
        "outputs_streamed": streamed,
        "result_outputs_empty": not result.outputs,
        "chunked_lane_native_admitted": disp["native_admitted"],
        "multi_chunk": disp["chunk_count"] > 1 if expect_chunks else True,
        "native_hash_columns": (
            disp["native_hash_columns"] == disp["hash_columns"] > 0 if companion else True
        ),
    }
    return {
        "execution_mode": execution.get("execution_mode"),
        "route_reason": execution.get("route_reason"),
        "outputs_streamed": streamed,
        "result_outputs": len(result.outputs),
        "sink_output_mode": qm["mixed_route"]["output"],
        "checks": checks,
        "ok": all(checks.values()),
        "disposition": disp,
    }


def _prepare(args: argparse.Namespace, work: Path) -> tuple[Path, dict[str, Any]]:
    from _mixed_layout import build_config, write_child

    child = work / "accounts.parquet"
    t0 = time.perf_counter()
    write_child(
        child,
        rows=args.child_rows,
        parents=args.parent_rows,
        orphan_every=args.orphan_every,
        width=args.width,
    )
    config = build_config(
        child,
        work / "cfg-targets",
        parents=args.parent_rows,
        width=args.width,
        rich_parent=args.rich_parent,
    )
    config["_build_s"] = round(time.perf_counter() - t0, 2)
    return child, config


def _pipeline_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    kw: dict[str, Any] = {"native_threads": args.native_threads}
    if args.chunk_rows:
        kw["chunk_size_rows"] = args.chunk_rows
    if args.chunk_threshold is not None:
        kw["auto_chunk_threshold_rows"] = args.chunk_threshold
    return kw


def _run_capacity(args: argparse.Namespace, work: Path) -> dict[str, Any]:
    import importlib.util

    from _mixed_layout import verify_committed
    from tests.parity._f2_mixed_support import key_provider

    from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
    from decoy_engine.profile._readers import LazySource

    child, config = _prepare(args, work)
    build_s = config.pop("_build_s")
    companion = importlib.util.find_spec("decoy_engine_native") is not None
    out = work / "out"
    committed = out / "committed"
    scratch = Path(tempfile.gettempdir())  # TMPDIR was pointed at this run's scratch dir
    scratch_sampler, stage_sampler = _DiskPeakSampler(scratch), _DiskPeakSampler(out)
    out.mkdir()
    probe = _MixedProbe()
    probe.install()
    rss_before = _status_mb("VmRSS")
    scratch_sampler.start()
    stage_sampler.start()
    t0 = time.perf_counter()
    try:
        result = run_pipeline(
            config,
            {"accounts": LazySource(path=child)},
            engine_version="f4-mixed-capacity",
            key_provider=key_provider(),
            execution_mode="auto",
            sink=ParquetTransactionalSink(committed),
            **_pipeline_kwargs(args),
        )
    finally:
        mask_s = time.perf_counter() - t0
        temp_disk_peak_mb = scratch_sampler.stop()
        staged_peak_mb = stage_sampler.stop()
    rss_hwm_after_run = _status_mb("VmHWM")

    expect_chunks = args.child_rows > (args.chunk_rows or 50_000)
    evidence = _route_evidence(result, expect_chunks, companion)
    verdict = verify_committed(
        committed,
        child,
        parents=args.parent_rows,
        child_rows=args.child_rows,
        orphan_every=args.orphan_every,
        width=args.width,
    )
    return {
        "completed_inner": bool(evidence["ok"] and verdict["status"] == "ok"),
        "mask_s": round(mask_s, 2),
        "build_s": build_s,
        "rows": {"people": args.parent_rows, "accounts": args.child_rows},
        "companion_present": companion,
        "route": evidence,
        "generation_phase": probe.data.get("generation"),
        "carrier_phase": probe.data.get("carrier"),
        "rss_before_run_mb": rss_before,
        "rss_hwm_after_run_mb": rss_hwm_after_run,
        "spill": {
            "temp_disk_peak_mb": round(temp_disk_peak_mb, 1),
            "staged_output_peak_mb": round(staged_peak_mb, 1),
        },
        "fk_parity": verdict,
        "committed_output_mb": round(_dir_mb(committed), 1),
        "rows_per_s": round((args.parent_rows + args.child_rows) / mask_s) if mask_s else None,
    }


def _run_oracle(args: argparse.Namespace, work: Path) -> dict[str, Any]:
    """Byte-identity of the production route to the full-frame oracle, one process."""
    import pyarrow.parquet as pq
    from tests.parity._f2_mixed_support import assert_outputs_identical, key_provider

    from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
    from decoy_engine.profile._readers import LazySource

    child, config = _prepare(args, work)
    config.pop("_build_s")
    sources = {"accounts": LazySource(path=child)}
    committed = work / "out" / "committed"
    kw = {**_pipeline_kwargs(args), "key_provider": key_provider(), "engine_version": "f4-oracle"}
    t0 = time.perf_counter()
    prod = run_pipeline(
        config, sources, execution_mode="auto", sink=ParquetTransactionalSink(committed), **kw
    )
    prod_s = time.perf_counter() - t0
    mode = prod.quality_metrics["execution"]["execution_mode"]
    got = {p.stem: pq.read_table(p) for p in sorted(committed.glob("*.parquet"))}
    del prod
    t0 = time.perf_counter()
    want = run_pipeline(config, sources, execution_mode="full_frame", **kw).outputs
    oracle_s = time.perf_counter() - t0
    identical, detail = True, None
    try:
        assert_outputs_identical(got, dict(want))
    except AssertionError as exc:
        identical, detail = False, f"{type(exc).__name__}: tables {sorted(got)} vs {sorted(want)}"
    return {
        "completed_inner": bool(identical and mode == "mixed"),
        "byte_identical": identical,
        "mismatch": detail,
        "production_mode": mode,
        "compared_tables": sorted(got),
        "rows": {t: v.num_rows for t, v in got.items()},
        "comparison": "canonical Arrow schema + Arrow IPC value bytes (not Parquet file identity)",
        "production_s": round(prod_s, 2),
        "oracle_s": round(oracle_s, 2),
    }


def _dir_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / _MIB


def run_child(args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parents[2]
    # This checkout's engine and fixtures win over another worktree's editable install.
    sys.path[:0] = [str(repo / "src"), str(repo)]
    result_path = Path(args.result_path)
    record: dict[str, Any] = {"completed_inner": False}
    try:
        run = _run_oracle if args.mode == "oracle" else _run_capacity
        record.update(run(args, Path(args.work_dir)))
    except BaseException as exc:
        record["error"] = _error_record(exc)
    record["worker_hwm_mb"] = _status_mb("VmHWM")
    tmp = result_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(result_path)  # atomic: the supervisor never sees a half-written file
    return 0 if record["completed_inner"] else 3


# -------------------------------------------------------------------------- supervisor


def _child_cmd(args: argparse.Namespace, work: Path, result_path: Path) -> list[str]:
    cmd = [
        sys.executable, str(Path(__file__).resolve()), "--child", "--mode", args.mode,
        "--work-dir", str(work), "--result-path", str(result_path),
        "--parent-rows", str(args.parent_rows), "--child-rows", str(args.child_rows),
        "--orphan-every", str(args.orphan_every), "--width", str(args.width),
        "--native-threads", str(args.native_threads),
    ]  # fmt: skip
    if args.rich_parent:
        cmd.append("--rich-parent")
    if args.chunk_rows:
        cmd += ["--chunk-rows", str(args.chunk_rows)]
    if args.chunk_threshold is not None:
        cmd += ["--chunk-threshold", str(args.chunk_threshold)]
    return cmd


def _one_run(args: argparse.Namespace, run_no: int) -> dict[str, Any]:
    work = Path(tempfile.mkdtemp(prefix=f"f4-mixed-{args.mode}{run_no}-", dir=args.work_root))
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
        failure = classify_failure(outcome)
        # Digested after the measured process exited; see `_f4a_gen.digest_in_subprocess`.
        digest = (
            _f4a_gen.digest_in_subprocess(work / "out" / "committed")
            if failure is None and args.rich_parent and args.mode == "capacity"
            else None
        )
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    return {
        "digest": digest,
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
    peak = max(r["peak_rss_mb"] for r in runs)
    base = {
        "completed": all(r["completed"] for r in runs),
        "failure": next((r["failure"] for r in runs if r["failure"]), None),
        "requested": {
            "mode": args.mode,
            "parent_rows": args.parent_rows,
            "child_rows": args.child_rows,
            "total_rows": args.parent_rows + args.child_rows,
            "orphan_every": args.orphan_every,
            "width_hash_payload": args.width,
            "chunk_rows": args.chunk_rows,
            "chunk_threshold": args.chunk_threshold,
            "native_threads": args.native_threads,
            "cpu_count": os.cpu_count(),
            "timeout_s": args.timeout_s,
            "rss_ceiling_mb": args.rss_ceiling_mb,
            "repeats": args.repeats,
        },
        "peak_rss_mb": peak,
        "peak_rss_poll_mb": max(r["peak_rss_poll_mb"] for r in runs),
        "rss_method": "wait4_ru_maxrss",
        "runs": [{k: v for k, v in r.items() if k != "result"} for r in runs],
    }
    if args.mode == "oracle":
        return {
            "schema": ORACLE_SCHEMA,
            **base,
            "capacity_figure": False,
            "oracle": {k: v for k, v in last.items() if k != "completed_inner"},
        }
    mask = [r["result"]["mask_s"] for r in runs if "mask_s" in r["result"]]
    return {
        "schema": SCHEMA,
        **base,
        "rows": last.get("rows"),
        "mask_s": mask[-1] if mask else None,
        "mask_s_p50": round(statistics.median(mask), 2) if mask else None,
        "mask_s_p95": round(_percentile(mask, 95), 2) if mask else None,
        "mask_s_runs": mask,
        "rss_within_ceiling": (
            (base["completed"] and peak <= args.rss_ceiling_mb) if args.rss_ceiling_mb else None
        ),
        "route": last.get("route"),
        "generation_phase": last.get("generation_phase"),
        "carrier_phase": last.get("carrier_phase"),
        "rss_before_run_mb": last.get("rss_before_run_mb"),
        "rss_hwm_after_run_mb": last.get("rss_hwm_after_run_mb"),
        "spill": last.get("spill"),
        "fk_parity": last.get("fk_parity"),
        "committed_output_mb": last.get("committed_output_mb"),
        "companion_present": last.get("companion_present"),
        "digest": runs[-1].get("digest"),
        "rows_per_s_last": last.get("rows_per_s"),
        "throughput_rows_per_s": (
            round((args.parent_rows + args.child_rows) / mask[-1]) if mask and mask[-1] else None
        ),
    }


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--mode", choices=("capacity", "oracle"), default="capacity")
    ap.add_argument("--parent-rows", type=int, required=True)
    ap.add_argument("--child-rows", type=int, required=True)
    ap.add_argument("--orphan-every", type=int, default=DEFAULT_ORPHAN_EVERY, help="0 = none")
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="hashed payload columns")
    ap.add_argument("--chunk-rows", type=int, default=None, help="lane chunk rows (engine default)")
    ap.add_argument("--chunk-threshold", type=int, default=None, help="auto-chunk threshold rows")
    ap.add_argument("--native-threads", type=int, default=1)
    ap.add_argument(
        "--rich-parent",
        action="store_true",
        help="F4a: generate the parent with every eligible family, and digest the output",
    )
    ap.add_argument(
        "--rss-ceiling-mb", type=float, default=None, help="declared acceptance ceiling"
    )
    ap.add_argument("--rss-kill-mb", type=float, default=None, help="supervisor safety kill")
    ap.add_argument("--timeout-s", type=float, default=None)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--work-root", default=None, help="parent dir for per-run scratch")
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
    if args.parent_rows < 1 or args.child_rows < 1:
        raise SystemExit("every table needs at least one row")
    record = supervise(args)
    line = json.dumps(record)
    print(f"BENCH_JSON {line}")
    if args.out:
        Path(args.out).write_text(line + "\n")
    return 0 if record["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
