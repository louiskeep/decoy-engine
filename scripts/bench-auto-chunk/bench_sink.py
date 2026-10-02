"""Driver for the B6a incremental-sink merge benchmark (plan 2026-10-02, Design 10).

Frozen method: both route families (`native`, `oracle`) at 2,000,000 and 10,000,000 rows;
per cell, three discarded warmup rounds and twenty measured rounds; each round runs the
resident (`stream_chunked_output=False`) and the streamed configuration once, in an order
shuffled by a `random.Random` seeded from `20261002` and the cell name; one fresh subprocess
per trial, one trial at a time, each trial under `/home/cam/.cache/pytest-one.lock` (the
machine-wide test lock, taken per trial so other test runs interleave between trials, never
during one); `native_threads=1`, `OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`. Percentiles
are nearest-rank (index `ceil(q * n)` of the sorted values). Bars and ceilings are computed
here from the plan's text (revision 2.2: the oracle-route wall p95 is directional, reported but
not a gate); this script never edits them.

Every trial is appended to `<out-dir>/results.jsonl` as it finishes, so a stopped run resumes
where it left off (a trial already in the file is skipped; the per-cell round order is
reproducible from the seed). A single cell runs with `--workloads oracle --rows 10000000`.

    bench_sink.py run --python PY --out-dir DIR --work-root DIR [--workloads ...] [--rows ...]
    bench_sink.py merge --out-dir DIR --saved results-3cells-saved.json [--saved ...] \
        [--merged-out merged.json]

`merge` combines previously saved cells (the `cells` object of an earlier `results.json`)
with the cells in `results.jsonl`, then evaluates every frozen bar over all four cells and
exits 0 only when each one passes.
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
WORKLOADS = ("native", "oracle")
SIZES = (2_000_000, 10_000_000)
MODES = ("resident", "streamed")


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
    """The machine-wide test lock, held for one trial."""
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_worker(py: str, args: list[str]) -> None:
    env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    with exclusive_lock():
        subprocess.run([py, str(WORKER), *args], check=True, env=env)  # noqa: S603


def trial(
    py: str, workload: str, rows: int, source: Path, mode: str, root: Path, tag: str
) -> dict[str, Any]:
    workdir = root / f"w_{tag}"
    out = root / f"{tag}.json"
    shutil.rmtree(workdir, ignore_errors=True)
    run_worker(
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
    evidence: dict[str, Any] = json.loads(out.read_text())
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


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _append(path: Path, entry: dict[str, Any]) -> None:
    with open(path, "a") as handle:
        handle.write(json.dumps(entry, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def cell_from_entries(entries: list[dict[str, Any]], cell: str) -> dict[str, Any] | None:
    """The cell's summary from its jsonl entries, or `None` while it is incomplete."""
    mine = [e for e in entries if e["cell"] == cell]
    refs = [e for e in mine if e["kind"] == "reference"]
    measured = {m: [e for e in mine if e["kind"] == "measured" and e["mode"] == m] for m in MODES}
    rounds = {m: {e["round"] for e in measured[m]} for m in MODES}
    if not refs or not rounds["resident"] or rounds["resident"] != rounds["streamed"]:
        return None
    ref = refs[0]["trial"]
    trials = {m: [e["trial"] for e in sorted(measured[m], key=lambda x: x["round"])] for m in MODES}
    return {
        "workload": ref["workload"],
        "rows": ref["rows"],
        "reference_sha256": ref["staged_sha256"],
        "source_nbytes": ref["source_nbytes"],
        "output_nbytes": ref["output_nbytes_resident"],
        "summary": {
            m: {
                "wall_s": summarize([t["wall_s"] for t in ts]),
                "increment_bytes": summarize([t["increment_bytes"] for t in ts]),
                "lifetime_peak_bytes": summarize([t["lifetime_peak_bytes"] for t in ts]),
                "arrow_pool_max_bytes": summarize([t["arrow_pool_max_bytes"] for t in ts]),
            }
            for m, ts in trials.items()
        },
        "trials": trials,
    }


def evaluate_bars(cells: dict[str, Any]) -> dict[str, Any]:
    """Every frozen bar over the cells present; a bar whose cells are missing is `pending`."""
    bars: dict[str, Any] = {}
    problems: list[str] = []
    for name, cell in cells.items():
        for mode in MODES:
            for t in cell["trials"][mode]:
                problems += [f"{name} {mode} round {t.get('round')}: {p}" for p in t["problems"]]
    for workload in WORKLOADS:
        for rows in SIZES:
            cell = cells.get(f"{workload}_{rows}")
            if cell is None:
                bars[f"W_{workload}_{rows}"] = {"pass": None, "status": "pending"}
                continue
            s = cell["summary"]
            wall = {
                "p50_ratio": s["streamed"]["wall_s"]["p50"] / s["resident"]["wall_s"]["p50"],
                "p95_ratio": s["streamed"]["wall_s"]["p95"] / s["resident"]["wall_s"]["p95"],
            }
            p95_ok = wall["p95_ratio"] <= 1.15
            if workload == "oracle":
                # Plan revision 2.2 (owner decision): the oracle-route p95 is directional. It
                # is reported with a sanity flag and does not set `pass`.
                wall["p95_directional_ok"] = p95_ok
                wall["p95_sanity_under_2x"] = wall["p95_ratio"] <= 2.0
                wall["pass"] = wall["p50_ratio"] <= 1.10
            else:
                wall["pass"] = wall["p50_ratio"] <= 1.10 and p95_ok
            bars[f"W_{workload}_{rows}"] = wall
        big, small = cells.get(f"{workload}_10000000"), cells.get(f"{workload}_2000000")
        if big is None or small is None:
            bars[f"M1_{workload}"] = {"pass": None, "status": "pending"}
            bars[f"M2_{workload}"] = {"pass": None, "status": "pending"}
            continue
        s10 = big["summary"]["streamed"]["increment_bytes"]["p95"]
        s2 = small["summary"]["streamed"]["increment_bytes"]["p95"]
        r10 = big["summary"]["resident"]["increment_bytes"]["p50"]
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
    bars["trial_problems"] = problems
    verdicts = [v["pass"] for v in bars.values() if isinstance(v, dict)]
    bars["all_pass"] = not problems and all(v is True for v in verdicts) and len(verdicts) == 8
    return bars


def run(args: argparse.Namespace) -> int:
    out_dir, root = Path(args.out_dir), Path(args.work_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    log = out_dir / "results.jsonl"
    meta = {
        "seed": SEED,
        "rounds": args.rounds,
        "warmups": args.warmups,
        "platform": platform.platform(),
        "cpu": _cpu_model(),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=1))
    for workload in args.workloads:
        for rows in args.rows:
            cell = f"{workload}_{rows}"
            source = root / f"{cell}.parquet"
            if not source.exists():
                run_worker(
                    args.python,
                    ["prepare", "--workload", workload, "--rows", str(rows), "--path", str(source)],
                )
            done = {
                (e["kind"], e.get("mode"), e.get("round"))
                for e in _load_jsonl(log)
                if e["cell"] == cell
            }
            if ("reference", "resident", None) not in done:
                ref = trial(args.python, workload, rows, source, "resident", root, f"{cell}_ref")
                _append(
                    log,
                    {
                        "cell": cell,
                        "kind": "reference",
                        "mode": "resident",
                        "round": None,
                        "trial": ref,
                    },
                )
            entries = [e for e in _load_jsonl(log) if e["cell"] == cell]
            reference = next(e for e in entries if e["kind"] == "reference")["trial"][
                "staged_sha256"
            ]
            rng = random.Random(f"{SEED}:{cell}")
            for rnd in range(args.warmups + args.rounds):
                order = list(MODES)
                rng.shuffle(order)
                for mode in order:
                    kind = "warmup" if rnd < args.warmups else "measured"
                    index = rnd - args.warmups if kind == "measured" else rnd
                    if (kind, mode, index) in done:
                        continue
                    ev = trial(args.python, workload, rows, source, mode, root, f"{cell}_{mode}")
                    ev["round"], ev["order"] = index, order
                    ev["problems"] = (
                        check_trial(ev, workload, rows, reference) if kind == "measured" else []
                    )
                    _append(
                        log, {"cell": cell, "kind": kind, "mode": mode, "round": index, "trial": ev}
                    )
                    print(
                        cell,
                        kind,
                        mode,
                        index,
                        round(ev["wall_s"], 2),
                        ev["increment_bytes"] // MIB,
                        flush=True,
                    )
    return 0


def merge(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    cells: dict[str, Any] = {}
    for saved in args.saved:
        cells.update(json.loads(Path(saved).read_text())["cells"])
    entries = _load_jsonl(out_dir / "results.jsonl")
    # The frozen method is 20 measured rounds. A cell deliberately run with fewer (`run
    # --rounds N`, recorded in meta.json) is merged at that N and the record must say so.
    meta_path = out_dir / "meta.json"
    needed = args.min_rounds or (
        json.loads(meta_path.read_text())["rounds"] if meta_path.exists() else 20
    )
    for cell in sorted({e["cell"] for e in entries}):
        built = cell_from_entries(entries, cell)
        if built is None:
            print(f"cell {cell} is incomplete in results.jsonl; not merged", file=sys.stderr)
            continue
        counts = {m: len(built["trials"][m]) for m in MODES}
        if min(counts.values()) < needed:
            print(f"cell {cell} has {counts} measured trials, need {needed} each", file=sys.stderr)
            continue
        cells[cell] = built
    bars = evaluate_bars(cells)
    merged = {"cells": cells, "bars": bars}
    target = Path(args.merged_out) if args.merged_out else out_dir / "merged.json"
    target.write_text(json.dumps(merged, indent=1, allow_nan=False))
    print(json.dumps(bars, indent=1))
    return 0 if bars["all_pass"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--python", required=True)
    runner.add_argument("--out-dir", required=True)
    runner.add_argument("--work-root", required=True)
    runner.add_argument("--rows", type=int, nargs="+", default=list(SIZES))
    runner.add_argument("--rounds", type=int, default=20)
    runner.add_argument("--warmups", type=int, default=3)
    runner.add_argument("--workloads", nargs="+", default=list(WORKLOADS), choices=WORKLOADS)
    runner.set_defaults(func=run)
    merger = sub.add_parser("merge")
    merger.add_argument("--out-dir", required=True)
    merger.add_argument("--saved", nargs="+", default=[])
    merger.add_argument("--merged-out")
    merger.add_argument("--min-rounds", type=int, default=0)
    merger.set_defaults(func=merge)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
