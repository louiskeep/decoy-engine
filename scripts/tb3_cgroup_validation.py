#!/usr/bin/env python3
"""TB-3: local cgroup-capped validation of the OOM-avoidance router (NO GCP spend).

`docs/plans/2026-07-12-track-b-completion-program.md` TB-3, and
`docs/plans/2026-07-10-oom-avoidance-routing-redesign.md`'s local fallback
(4-6M rows under a cgroup cap on the devbox reproduces the ratio/parity shape).
The confidence gate BEFORE enabling flags (TB-5) and BEFORE paying for GCP
(TB-6). Results writeup: `docs/plans/2026-07-13-tb3-cgroup-validation-results.md`.

Unlike TB-2's `tests/perf/test_governor_reroute_completion.py` -- which proves
reroute under an IN-PROCESS RSS monitor (the governor SIGKILLs its own child
before the kernel would) -- TB-3 runs the SAME machinery under a REAL kernel
`memory.max` cgroup v2 cap (a transient `systemd-run` service), so the ceiling
is enforced by the kernel OOM killer, not our watchdog. That is the point: it
proves the design survives a real hardware/container memory limit like the ones
production (k8s / cgroup-limited VMs) imposes.

Three proofs the design's §9 acceptance requires:
  1. ROUTE SELECTION BY BYTES vs BUDGET (a WIDTH-change test, NOT row-count):
     same row count, wider schema (more bytes/row) pushes the router
     full_frame -> out_of_core because the byte estimate crosses the budget.
  2. REROUTE-TO-COMPLETION UNDER THE CAP: a job whose full_frame peak exceeds
     the cgroup cap COMPLETES via out_of_core under that SAME cap (no wedge, no
     whole-job OOM-kill).
  3. PATH PARITY (byte-identity): for a job small enough to run both routes,
     the masked output is content-identical between full_frame and out_of_core.

Proof-of-bite: before trusting any reroute result, a deliberately too-big job
under a tight cap is shown to be cgroup-OOM-KILLED, proving the cap enforces.

MANUAL / GATED, NOT a default-CI test: needs cgroup v2 + passwordless `sudo
systemd-run` and minutes of runtime. Run it directly:

    .venv/bin/python scripts/tb3_cgroup_validation.py

`--worker` is the internal capped-child entrypoint; the default (orchestrate)
mode spawns those children under real caps and aggregates their JSON.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import resource
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

_REPO_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# Fixture: pure-mask parent -> child FK job, out-of-core-eligible by
# construction (hash/redact/truncate are all out-of-core-supported strategies,
# `out_of_core/_compat.py`; parent.id and child.pid share the `pns` hash
# namespace so masking preserves the FK edge). This is the same shape TB-2
# proved with; TB-3 scales it up and runs it under a real kernel cap.
# --------------------------------------------------------------------------

_MB = 1024 * 1024
_HASH_NS = "pns"


def _payload_columns(n_rows: int, n_cols: int, width: int, tag: str) -> dict[str, pa.Array]:
    """`n_cols` variable-width string columns, each cell ~`width` bytes -- the
    knob that changes bytes/row WITHOUT changing row count (proof 1's width
    lever), and the bulk of full_frame's resident footprint (proof 2's scale
    lever)."""
    cols: dict[str, pa.Array] = {}
    pad = "x" * max(0, width)
    for c in range(n_cols):
        cols[f"{tag}{c}"] = pa.array(
            [f"{tag}{c}-{i}-{pad}" for i in range(n_rows)], type=pa.string()
        )
    return cols


def build_fixture(
    work: Path, *, n_rows: int, n_payload_cols: int, payload_width: int
) -> dict[str, Any]:
    """Write parent/child parquet to `work` and return the run config.

    `n_payload_cols` * `payload_width` sets bytes/row; `n_rows` sets scale.
    Masking: parent.id (hash, pns), child.pid (hash, pns -> preserves the FK
    edge), plus redact/truncate payload columns so real transform work runs.
    """
    parent_cols: dict[str, pa.Array] = {
        "id": pa.array([f"p{i}" for i in range(n_rows)], type=pa.string()),
    }
    parent_cols.update(_payload_columns(n_rows, n_payload_cols, payload_width, "pnote"))
    parent = pa.table(parent_cols)

    child_cols: dict[str, pa.Array] = {
        "cid": pa.array([f"c{i}" for i in range(n_rows)], type=pa.string()),
        # exact bijection with parent.id[i] -> post-mask FK check is exact
        "pid": pa.array([f"p{i % n_rows}" for i in range(n_rows)], type=pa.string()),
    }
    child_cols.update(_payload_columns(n_rows, n_payload_cols, payload_width, "ccode"))
    child = pa.table(child_cols)

    for name, tbl in (("parent", parent), ("child", child)):
        pq.write_table(tbl, work / f"{name}.parquet")

    parent_columns: list[dict[str, Any]] = [
        {"name": "id", "strategy": "hash", "namespace": _HASH_NS}
    ]
    parent_columns += [{"name": f"pnote{c}", "strategy": "redact"} for c in range(n_payload_cols)]
    child_columns: list[dict[str, Any]] = [
        {"name": "cid", "strategy": "hash", "namespace": "cns"},
        {"name": "pid", "strategy": "hash", "namespace": _HASH_NS},
    ]
    child_columns += [
        {"name": f"ccode{c}", "strategy": "truncate", "provider_config": {"length": 4}}
        for c in range(n_payload_cols)
    ]
    return {
        "version": 1,
        "global_settings": {"job_name": "tb3-cgroup-validation", "seed": 11},
        "sources": {
            name: {
                "type": "file",
                "path": str(work / f"{name}.parquet"),
                "format": "parquet",
            }
            for name in ("parent", "child")
        },
        "targets": {
            name: {
                "type": "file",
                "path": str(work / f"{name}.out.parquet"),
                "format": "parquet",
            }
            for name in ("parent", "child")
        },
        "tables": [
            {"name": "parent", "columns": parent_columns},
            {"name": "child", "columns": child_columns},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": _HASH_NS,
            },
        ],
    }


# Worker: runs INSIDE a capped systemd service. Prints exactly one JSON line.


def _peak_rss_mb() -> float:
    """This process's high-water RSS (ru_maxrss is KiB on Linux)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _content_hash(outputs: dict[str, pa.Table]) -> dict[str, str]:
    """Order-sensitive content hash per table (proof 3 parity). Uses
    `to_pydict()` -- the SAME canonical form the existing
    `tests/parity/test_out_of_core_routing_parity.py` compares with -- so a
    hash match here is exactly that test's byte-identity claim at scale."""
    out: dict[str, str] = {}
    for name, tbl in sorted(outputs.items()):
        h = hashlib.sha256()
        h.update(name.encode())
        for col_name, col in sorted(tbl.to_pydict().items()):
            h.update(b"\x00" + col_name.encode() + b"\x00")
            h.update(repr(col).encode())
        out[name] = h.hexdigest()
    return out


def _load_resident(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _load_lazy(config: dict[str, Any]) -> dict[str, Any]:
    from decoy_engine.profile._readers import LazySource

    return {name: LazySource(Path(spec["path"])) for name, spec in config["sources"].items()}


def _fk_consistency(outputs: dict[str, pa.Table], n_rows: int) -> dict[str, Any]:
    parent_ids = outputs["parent"].column("id").to_pylist()
    child_pids = outputs["child"].column("pid").to_pylist()
    return {
        "rows_parent": len(parent_ids),
        "rows_child": len(child_pids),
        "fk_edge_preserved": child_pids == parent_ids,
        "masking_transformed": all(pid != f"p{i}" for i, pid in enumerate(parent_ids)),
        "ids_distinct": len(set(parent_ids)) == n_rows,
    }


def worker_route(config: dict[str, Any], *, mode: str, n_rows: int, budget_bytes: int) -> dict:
    """Run one forced route in-process (full_frame resident / out_of_core lazy)
    and report peak + outcome + content hash + FK consistency."""
    from decoy_engine.execution._pipeline import run_pipeline

    if mode == "full_frame":
        sources = _load_resident(config)
        result = run_pipeline(config, sources, engine_version="tb3", execution_mode="full_frame")
    elif mode == "out_of_core":
        # LazySource so input is NOT fully resident: the route stays bounded.
        sources = _load_lazy(config)
        result = run_pipeline(
            config,
            sources,
            engine_version="tb3",
            execution_mode="out_of_core",
            out_of_core_budget_bytes=budget_bytes,
        )
    else:  # pragma: no cover - guarded by argparse choices
        raise ValueError(mode)

    outputs = result.outputs
    return {
        "mode": mode,
        "outcome": "completed",
        "execution_mode": result.quality_metrics["execution"]["execution_mode"],
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        "content_hash": _content_hash(outputs),
        "fk": _fk_consistency(outputs, n_rows),
    }


def worker_governor(config: dict[str, Any], *, n_rows: int, budget_bytes: int) -> dict:
    """Run the governor ladder (full_frame -> out_of_core -> sequential) with a
    budget set to the cgroup cap, so the in-process monitor trips full_frame
    BELOW the kernel ceiling and reroutes to a completed out_of_core run --
    the reroute-to-completion property, now under a real kernel cap."""
    from decoy_engine.execution._governor import run_job_with_governor

    sources = _load_resident(config)
    result = run_job_with_governor(
        config,
        sources,
        budget_bytes=budget_bytes,
        use_runtime_governor=True,
        poll_interval_s=0.1,
        engine_version="tb3",
    )
    trips = [
        {
            "route": t.route,
            "trip_kind": t.trip_kind,
            "observed_peak_mb": t.observed_peak_mb,
            "reroute_to": t.reroute_to,
        }
        for t in result.trips
    ]
    payload: dict[str, Any] = {
        "mode": "governor",
        "outcome": result.outcome,
        "final_route": result.final_route,
        "trips": trips,
        "peak_rss_mb": result.result.peak_rss_mb if result.result is not None else None,
        "diagnostic": result.diagnostic,
    }
    if result.result is not None and result.result.outputs is not None:
        payload["execution_mode"] = (result.result.quality_metrics.get("execution") or {}).get(
            "execution_mode"
        )
        payload["fk"] = _fk_consistency(result.result.outputs, n_rows)
    return payload


def worker_route_decision(config: dict[str, Any], *, budget_bytes: int | None) -> dict:
    """Run AUTO routing with byte-estimate routing ON and a fixed budget (the
    detected cgroup slot budget when `budget_bytes` is None). The chosen route
    is what proof 1 reads: narrow vs wide (same rows) flips full_frame ->
    out_of_core purely because the byte estimate crosses the budget."""
    from decoy_engine.execution._pipeline import run_pipeline

    sources = _load_resident(config)
    result = run_pipeline(
        config,
        sources,
        engine_version="tb3",
        execution_mode="auto",
        use_byte_estimate_routing=True,
        out_of_core_budget_bytes=budget_bytes,
    )
    return {
        "mode": "route_decision",
        "outcome": "completed",
        "execution_mode": result.quality_metrics["execution"]["execution_mode"],
        "peak_rss_mb": round(_peak_rss_mb(), 1),
    }


def run_worker(args: argparse.Namespace) -> int:
    config = json.loads(Path(args.config).read_text())
    if args.worker == "route_decision":
        budget = args.budget_bytes if args.budget_bytes > 0 else None
        payload = worker_route_decision(config, budget_bytes=budget)
    elif args.worker == "governor":
        payload = worker_governor(config, n_rows=args.rows, budget_bytes=args.budget_bytes)
    else:
        payload = worker_route(
            config, mode=args.worker, n_rows=args.rows, budget_bytes=args.budget_bytes
        )
    sys.stdout.write("TB3_JSON:" + json.dumps(payload) + "\n")
    return 0


# --------------------------------------------------------------------------
# Orchestrator: spawns capped children, aggregates, prints the three proofs.
# --------------------------------------------------------------------------


def _preflight() -> None:
    """Fail LOUD and precise if a real kernel cap is not achievable here --
    TB-3's whole value is the real cap; never silently degrade to the
    in-process monitor and call it TB-3."""
    problems = []
    try:
        cg = Path("/sys/fs/cgroup/cgroup.controllers").read_text()
        if "memory" not in cg.split():
            problems.append("cgroup v2 'memory' controller absent at /sys/fs/cgroup")
    except OSError as exc:
        problems.append(f"cannot read cgroup v2 controllers (not cgroup v2?): {exc}")
    if shutil.which("systemd-run") is None:
        problems.append("systemd-run not on PATH")
    if subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True).returncode != 0:
        problems.append("passwordless `sudo -n` unavailable (needed for systemd-run service)")
    if problems:
        sys.stderr.write(
            "TB-3 ABORT: a real kernel memory.max cap is NOT achievable here:\n  - "
            + "\n  - ".join(problems)
            + "\nTB-3 requires a real cgroup v2 cap; refusing to fall back to the "
            "in-process RSS monitor.\n"
        )
        raise SystemExit(3)


def _capped_service_cmd(cap_mb: int, argv: list[str]) -> list[str]:
    """`systemd-run` transient SERVICE under a hard cgroup v2 `memory.max`.

    A transient SERVICE (not `--scope`): `--scope` runs synchronously in the
    caller context and does NOT cleanly host a workload that spawns a
    supervised child subprocess (the governor's isolated worker) -- it SIGTERMs
    the tree early. A service reparents to the systemd manager, so the
    governor's spawn/kill/reroute lifecycle runs intact. `User=` runs it as the
    invoking non-root user (the `memory.max` cap is a cgroup property enforced
    regardless of UID) so fixtures/outputs stay user-owned; `MemorySwapMax=0`
    makes the cap a true RAM ceiling.

    `OOMPolicy=continue` is ESSENTIAL: systemd's default `OOMPolicy=stop` tears
    down the WHOLE unit (killing the governor parent) the instant the kernel
    OOM-kills ANY member -- exactly the full_frame child the governor WANTS
    killed so it can reroute. With `continue`, the kernel kills the over-cap
    full_frame child (largest RSS), the parent survives, and the reroute
    proceeds. Harmless for the single-process workers."""
    user = getpass.getuser()
    props = [
        f"MemoryMax={cap_mb}M",
        "MemorySwapMax=0",
        "OOMPolicy=continue",
        f"User={user}",
        f"Group={user}",
        f"WorkingDirectory={_REPO_ROOT}",
    ]
    cmd = ["sudo", "-n", "systemd-run", "--wait", "--pipe", "--collect", "--quiet"]
    for prop in props:
        cmd += ["-p", prop]
    return [*cmd, *argv]


def _run_capped(cap_mb: int, worker: str, config: Path, *, rows: int, budget_bytes: int) -> tuple:
    """Run one worker inside a hard `memory.max` service. Returns (exit_code,
    parsed_json_or_None). A cgroup-OOM kill surfaces as a non-zero exit with no
    JSON (exit 137, or -SIGKILL via the service boundary)."""
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        worker,
        "--config",
        str(config),
        "--rows",
        str(rows),
        "--budget-bytes",
        str(budget_bytes),
    ]
    proc = subprocess.run(_capped_service_cmd(cap_mb, argv), capture_output=True, text=True)
    payload = None
    for line in proc.stdout.splitlines():
        if line.startswith("TB3_JSON:"):
            payload = json.loads(line[len("TB3_JSON:") :])
    return proc.returncode, payload


def _verify_cap_bites(cap_mb: int) -> dict[str, Any]:
    """Control: a deliberately-too-big allocation under a tight cap MUST be
    cgroup-OOM-killed. Proves the cap actually enforces before we trust any
    reroute result under it."""
    alloc_mb = cap_mb * 4
    code = (
        f"b=bytearray({alloc_mb}*1024*1024)\n"
        "for i in range(0,len(b),4096): b[i]=1\n"
        "print('NOT_KILLED')\n"
    )
    proc = subprocess.run(
        _capped_service_cmd(cap_mb, [sys.executable, "-c", code]),
        capture_output=True,
        text=True,
    )
    # A cgroup-OOM kill surfaces as a non-zero `systemd-run --wait` exit (255
    # for an OOM-killed service main process, observed on this host; 137 or a
    # negative SIGKILL on other setups). The decisive signal in every case: the
    # allocation did NOT print its success marker.
    killed = proc.returncode != 0 and "NOT_KILLED" not in proc.stdout
    return {"cap_mb": cap_mb, "alloc_mb": alloc_mb, "exit_code": proc.returncode, "killed": killed}


def orchestrate(args: argparse.Namespace) -> int:
    _preflight()
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"scale": {}, "proofs": {}}

    # --- Mechanism proof-of-bite ------------------------------------------
    bite = _verify_cap_bites(64)
    report["cap_bite"] = bite

    # === Proof 2: reroute-to-completion under a real cap ==================
    p2_dir = work / "p2"
    p2_dir.mkdir(exist_ok=True)
    cfg2 = p2_dir / "config.json"
    config2 = build_fixture(
        p2_dir, n_rows=args.rows, n_payload_cols=args.cols, payload_width=args.width
    )
    cfg2.write_text(json.dumps(config2))
    report["scale"] = {
        "rows": args.rows,
        "payload_cols": args.cols,
        "payload_width_bytes": args.width,
        "cap_mb": args.cap_mb,
    }
    cap_bytes = args.cap_mb * _MB

    # baseline peaks (generous cap, no kill) to size the cap sanity-check
    _, ff_free = _run_capped(
        args.headroom_mb, "full_frame", cfg2, rows=args.rows, budget_bytes=cap_bytes
    )
    _, ooc_free = _run_capped(
        args.headroom_mb, "out_of_core", cfg2, rows=args.rows, budget_bytes=cap_bytes
    )

    # full_frame under the TIGHT cap: must be cgroup-OOM-killed (137)
    ff_capped_code, ff_capped = _run_capped(
        args.cap_mb, "full_frame", cfg2, rows=args.rows, budget_bytes=cap_bytes
    )
    # out_of_core under the SAME tight cap: must complete
    _, ooc_capped = _run_capped(
        args.cap_mb, "out_of_core", cfg2, rows=args.rows, budget_bytes=cap_bytes
    )
    # governor under the SAME cap, budget == cap: trips full_frame, reroutes,
    # completes as out_of_core
    _, gov = _run_capped(args.cap_mb, "governor", cfg2, rows=args.rows, budget_bytes=cap_bytes)

    report["proofs"]["p2_reroute_to_completion"] = {
        "full_frame_peak_free_mb": (ff_free or {}).get("peak_rss_mb"),
        "out_of_core_peak_free_mb": (ooc_free or {}).get("peak_rss_mb"),
        "full_frame_under_cap_exit": ff_capped_code,
        # cgroup-OOM kill surfaces as a non-zero service exit (255 here); the
        # decisive signal is that full_frame produced no completion envelope
        # under the tight cap.
        "full_frame_under_cap_killed": ff_capped_code != 0 and ff_capped is None,
        "out_of_core_under_cap": ooc_capped,
        "governor_under_cap": gov,
    }

    # === Proof 3: full_frame vs out_of_core path parity (byte-identity) ====
    # Small enough to run BOTH routes to completion under a generous cap.
    p3_dir = work / "p3"
    p3_dir.mkdir(exist_ok=True)
    cfg3 = p3_dir / "config.json"
    config3 = build_fixture(p3_dir, n_rows=args.parity_rows, n_payload_cols=2, payload_width=40)
    cfg3.write_text(json.dumps(config3))
    _, ff3 = _run_capped(
        args.headroom_mb, "full_frame", cfg3, rows=args.parity_rows, budget_bytes=cap_bytes
    )
    _, ooc3 = _run_capped(
        args.headroom_mb, "out_of_core", cfg3, rows=args.parity_rows, budget_bytes=cap_bytes
    )
    parity_match = bool(ff3 and ooc3 and ff3["content_hash"] == ooc3["content_hash"])
    report["proofs"]["p3_path_parity"] = {
        "parity_rows": args.parity_rows,
        "full_frame_hash": (ff3 or {}).get("content_hash"),
        "out_of_core_hash": (ooc3 or {}).get("content_hash"),
        "byte_identical": parity_match,
        "full_frame_execution_mode": (ff3 or {}).get("execution_mode"),
        "out_of_core_execution_mode": (ooc3 or {}).get("execution_mode"),
    }

    # === Proof 1: route selection by BYTES vs budget (width test) ==========
    # SAME row count; only bytes/row changes. Budget forwarded explicitly so
    # the flip is a pure bytes-vs-budget decision. Narrow -> full_frame,
    # wide -> out_of_core.
    p1_budget = args.width_budget_mb * _MB
    narrow_dir = work / "p1_narrow"
    wide_dir = work / "p1_wide"
    narrow_dir.mkdir(exist_ok=True)
    wide_dir.mkdir(exist_ok=True)
    cfg_narrow = narrow_dir / "config.json"
    cfg_wide = wide_dir / "config.json"
    cfg_narrow.write_text(
        json.dumps(
            build_fixture(narrow_dir, n_rows=args.width_rows, n_payload_cols=1, payload_width=16)
        )
    )
    cfg_wide.write_text(
        json.dumps(
            build_fixture(wide_dir, n_rows=args.width_rows, n_payload_cols=10, payload_width=80)
        )
    )
    _, narrow = _run_capped(
        args.headroom_mb, "route_decision", cfg_narrow, rows=args.width_rows, budget_bytes=p1_budget
    )
    _, wide = _run_capped(
        args.headroom_mb, "route_decision", cfg_wide, rows=args.width_rows, budget_bytes=p1_budget
    )
    report["proofs"]["p1_route_by_bytes"] = {
        "rows_held_constant": args.width_rows,
        "budget_mb": args.width_budget_mb,
        "narrow_route": (narrow or {}).get("execution_mode"),
        "wide_route": (wide or {}).get("execution_mode"),
        "flips_on_width": bool(
            narrow
            and wide
            and narrow["execution_mode"] == "full_frame"
            and wide["execution_mode"] == "out_of_core"
        ),
    }

    print(json.dumps(report, indent=2))
    _print_verdict(report)
    return 0


def _print_verdict(report: dict[str, Any]) -> None:
    p1 = report["proofs"]["p1_route_by_bytes"]["flips_on_width"]
    p2s = report["proofs"]["p2_reroute_to_completion"]
    gov = p2s.get("governor_under_cap") or {}
    p2 = (
        p2s["full_frame_under_cap_killed"]
        and gov.get("outcome") == "completed"
        and gov.get("final_route") == "out_of_core"
    )
    p3 = report["proofs"]["p3_path_parity"]["byte_identical"]
    print("\n===== TB-3 VERDICT =====")
    print(f"  cap-bites control:            {'PASS' if report['cap_bite']['killed'] else 'FAIL'}")
    print(f"  proof 1 route-by-bytes:       {'PASS' if p1 else 'FAIL'}")
    print(f"  proof 2 reroute-to-complete:  {'PASS' if p2 else 'FAIL'}")
    print(f"  proof 3 path parity:          {'PASS' if p3 else 'FAIL'}")
    if not (report["cap_bite"]["killed"] and p1 and p2 and p3):
        raise SystemExit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description="TB-3 local cgroup-capped validation")
    ap.add_argument(
        "--worker",
        choices=["full_frame", "out_of_core", "governor", "route_decision"],
        default=None,
        help="internal capped-child entrypoint (omit for orchestrate mode)",
    )
    ap.add_argument("--config", type=str, default=None)
    ap.add_argument("--rows", type=int, default=750_000)
    ap.add_argument("--cols", type=int, default=6, help="payload string cols/table (scale)")
    ap.add_argument("--width", type=int, default=64, help="bytes per payload cell (scale)")
    ap.add_argument("--cap-mb", type=int, default=2400, help="tight kernel cap for proof 2 (MB)")
    ap.add_argument("--headroom-mb", type=int, default=6000, help="generous cap for baselines (MB)")
    ap.add_argument("--budget-bytes", type=int, default=0)
    ap.add_argument("--parity-rows", type=int, default=2000)
    ap.add_argument("--width-rows", type=int, default=200_000)
    ap.add_argument("--width-budget-mb", type=int, default=500)
    ap.add_argument(
        "--work-dir",
        type=str,
        default="/tmp/claude-1000/-home-cam/scratch/tb3",
    )
    args = ap.parse_args()
    if args.worker:
        return run_worker(args)
    return orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
