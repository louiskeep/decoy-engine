"""Driver for the B6b LazySource merge benchmark (plan 2026-10-02-b6b-lazy-batch-input, section 10).

Cells (each its own invocation, each trial a fresh subprocess under the machine test lock
`/home/cam/.cache/pytest-one.lock`, taken per trial and never for the whole cell, so other test
runs interleave between trials):

    native_2M, native_10M      configs a (resident baseline) and b (LazySource candidate),
                               1 warmup + 10 measured each
    oracle_2M, oracle_10M      the same, with fewer rounds by owner decision (pandas-path
                               minimal rule): 1 warmup + 5 measured; wall p95 on the oracle
                               route is directional, every memory bar stays strict
    native_50M, oracle_50M     config b, 3 measured; one unmeasured config a run is the reference
    native_10M_onegroup        config b, 3 measured, the 10M source written as one row group

Each trial is appended to `<out-dir>/results.jsonl` keyed by `(cell, config, kind, index)`; a
cell invocation skips trials already recorded, so an interrupted cell resumes. Within a round
the two configs run in an order shuffled by `random.Random(f"20261002:{cell}")`. `summarize`
computes every bar from the file; this script never edits a bar. A 50M cell needs 20 GiB free.

    bench_lazy.py run --cell native_10M --python PY --out-dir DIR --work-root DIR
    bench_lazy.py summarize --out-dir DIR
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import random
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
WORKER = HERE / "bench_worker_lazy.py"
LOCK = "/home/cam/.cache/pytest-one.lock"
SEED = 20261002
GIB = 1024**3
MIB = 1024**2
ROW_GROUP_ROWS = 1_048_576
CEILING = int(1.5 * GIB)
BASELINE_ABORT = 8 * GIB

# cell -> (workload, rows, one_group, configs, warmups, measured, needs_reference_run)
CELLS: dict[str, tuple[str, int, bool, tuple[str, ...], int, int, bool]] = {
    "native_2M": ("native", 2_000_000, False, ("a", "b"), 1, 10, False),
    "native_10M": ("native", 10_000_000, False, ("a", "b"), 1, 10, False),
    "native_50M": ("native", 50_000_000, False, ("b",), 0, 3, True),
    "oracle_2M": ("oracle", 2_000_000, False, ("a", "b"), 1, 5, False),
    "oracle_10M": ("oracle", 10_000_000, False, ("a", "b"), 1, 5, False),
    "oracle_50M": ("oracle", 50_000_000, False, ("b",), 0, 3, True),
    "native_10M_onegroup": ("native", 10_000_000, True, ("b",), 0, 3, False),
}


def nearest_rank(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def stats(values: list[float]) -> dict[str, float]:
    return {"p50": nearest_rank(values, 0.5), "p95": nearest_rank(values, 0.95), "max": max(values)}


@contextlib.contextmanager
def exclusive_lock() -> Iterator[None]:
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def worker(py: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    with exclusive_lock():
        return subprocess.run(  # noqa: S603
            [py, str(WORKER), *args], check=True, env=env, capture_output=True, text=True
        )


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _append(path: Path, entry: dict[str, Any]) -> None:
    with open(path, "a") as handle:
        handle.write(json.dumps(entry, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def check(
    ev: dict[str, Any], config: str, workload: str, rows: int, reference: str | None
) -> list[str]:
    problems: list[str] = []
    if ev["probe_ran"]:
        problems.append("the routing probe ran")
    if reference is not None and ev["staged_sha256"] != reference:
        problems.append("staged file differs from the reference bytes")
    if ev["staged_tables"] != ["t"]:
        problems.append(f"staged tables {ev['staged_tables']}")
    if workload == "native" and ev["native_admitted"] is not True:
        problems.append("native workload did not run native")
    if workload == "oracle" and ev["native_admitted"] is not False:
        problems.append("oracle workload did not reroute to the oracle")
    if config == "b":
        out = ev["output"] or {}
        inp = ev["input"] or {}
        if inp.get("mode") != "lazy":
            problems.append(f"input mode {inp.get('mode')}")
        if ev["outputs_streamed"] is not True or out.get("mode") != "streamed":
            problems.append("run did not stream")
        if out.get("byte_cut_row_groups") != 0:
            problems.append("a row group was cut by the byte cap")
        if ev["loaded_fully_in_memory"] is not False:
            problems.append("loaded_fully_in_memory is not False")
        if ev["lifetime_peak_bytes"] > CEILING:
            problems.append(f"lifetime peak {ev['lifetime_peak_bytes']} over the 1.5 GiB ceiling")
    elif ev["lifetime_peak_bytes"] > BASELINE_ABORT:
        problems.append("baseline over the 8 GiB safety abort")
    return problems


def prepare(py: str, cell: str, root: Path, out_dir: Path) -> Path:
    workload, rows, one_group, *_ = CELLS[cell]
    path = root / f"{cell}.parquet"
    if path.exists():
        return path
    if rows >= 50_000_000 and shutil.disk_usage(root).free < 20 * GIB:
        raise SystemExit("less than 20 GiB free; refusing to start a 50M cell")
    args = ["prepare", "--workload", workload, "--rows", str(rows), "--path", str(path)]
    if one_group:
        args.append("--one-group")
    done = worker(py, args)
    info = json.loads(done.stdout.strip().splitlines()[-1])
    _append(out_dir / "sources.jsonl", {"cell": cell, **info})
    return path


def run_trial(
    py: str, cell: str, config: str, source: Path, root: Path, tag: str
) -> dict[str, Any]:
    workload, rows, *_ = CELLS[cell]
    workdir, out = root / f"w_{tag}", root / f"{tag}.json"
    shutil.rmtree(workdir, ignore_errors=True)
    worker(
        py,
        [
            "run", "--workload", workload, "--rows", str(rows), "--source", str(source),
            "--config", config, "--workdir", str(workdir), "--out", str(out),
        ],
    )  # fmt: skip
    ev: dict[str, Any] = json.loads(out.read_text())
    shutil.rmtree(workdir, ignore_errors=True)
    return ev


def run(args: argparse.Namespace) -> int:
    cell = args.cell
    workload, rows, _one, configs, warmups, measured, needs_ref = CELLS[cell]
    out_dir, root = Path(args.out_dir), Path(args.work_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    log = out_dir / "results.jsonl"
    source = prepare(args.python, cell, root, out_dir)
    seen = {(e["config"], e["kind"], e["index"]) for e in _load(log) if e["cell"] == cell}

    def reference() -> str | None:
        entries = _load(log)
        own = [e for e in entries if e["cell"] == cell and e["config"] == "a"]
        if own:
            return str(own[0]["trial"]["staged_sha256"])
        if cell == "native_10M_onegroup":
            base = [e for e in entries if e["cell"] == "native_10M" and e["config"] == "a"]
            return str(base[0]["trial"]["staged_sha256"]) if base else None
        return None

    def record(config: str, kind: str, index: int) -> None:
        if (config, kind, index) in seen:
            return
        ev = run_trial(args.python, cell, config, source, root, f"{cell}_{config}")
        ev["problems"] = check(
            ev, config, workload, rows, reference() if kind != "reference" else None
        )
        _append(log, {"cell": cell, "config": config, "kind": kind, "index": index, "trial": ev})
        print(
            cell,
            config,
            kind,
            index,
            round(ev["wall_s"], 2),
            ev["increment_bytes"] // MIB,
            flush=True,
        )
        # Plan section 10: a trial problem (the routing probe ran, a staged-bytes
        # mismatch, the wrong admission, a non-streaming candidate or a memory
        # ceiling breach) stops the cell for the owner rather than letting the
        # cell finish on tainted trials. The trial is already recorded above, so
        # the stop is auditable and the cell resumes after the cause is fixed.
        if ev["problems"]:
            raise SystemExit(f"STOP: {cell} {config}/{kind}/{index} problems: {ev['problems']}")

    if needs_ref:
        record("a", "reference", 0)
    rng = random.Random(f"{SEED}:{cell}")
    for rnd in range(warmups + measured):
        order = list(configs)
        rng.shuffle(order)
        kind = "warmup" if rnd < warmups else "measured"
        index = rnd if kind == "warmup" else rnd - warmups
        for config in order:
            record(config, kind, index)
    return 0


def cell_trials(entries: list[dict[str, Any]], cell: str, config: str) -> list[dict[str, Any]]:
    return [
        e["trial"]
        for e in entries
        if e["cell"] == cell and e["config"] == config and e["kind"] == "measured"
    ]


def summarize(args: argparse.Namespace) -> int:
    entries = _load(Path(args.out_dir) / "results.jsonl")
    out: dict[str, Any] = {"cells": {}, "bars": {}, "problems": []}
    for cell in CELLS:
        for config in ("a", "b"):
            trials = cell_trials(entries, cell, config)
            if not trials:
                continue
            out["cells"].setdefault(cell, {})[config] = {
                "n": len(trials),
                "wall_s": stats([t["wall_s"] for t in trials]),
                "increment_bytes": stats([t["increment_bytes"] for t in trials]),
                "lifetime_peak_bytes": stats([t["lifetime_peak_bytes"] for t in trials]),
            }
            for t in trials:
                out["problems"] += [f"{cell} {config}: {p}" for p in t["problems"]]
    cells, bars = out["cells"], out["bars"]

    def inc(cell: str, config: str, key: str) -> float | None:
        c = cells.get(cell, {}).get(config)
        return None if c is None else float(c["increment_bytes"][key])

    for family in ("native", "oracle"):
        s2, s10, s50 = (
            inc(f"{family}_{n}", "b", k) for n, k in (("2M", "p95"), ("10M", "p95"), ("50M", "max"))
        )
        b2, b10 = inc(f"{family}_2M", "a", "p50"), inc(f"{family}_10M", "a", "p50")
        if s2 is not None:
            limit = max(1.25 * s2, s2 + 128 * MIB)
            if s10 is not None:
                bars[f"M2_{family}_10M"] = {"value": s10, "limit": limit, "pass": s10 <= limit}
            if s50 is not None:
                bars[f"M2_{family}_50M"] = {"value": s50, "limit": limit, "pass": s50 <= limit}
            if b2 is not None:
                bars[f"Mbase_{family}"] = {
                    "value": s2,
                    "limit": b2 + 64 * MIB,
                    "pass": s2 <= b2 + 64 * MIB,
                }
        if s10 is not None and b10 is not None:
            bars[f"M1_{family}"] = {"value": s10, "limit": 0.5 * b10, "pass": s10 <= 0.5 * b10}
        for n in ("2M", "10M"):
            a, b = cells.get(f"{family}_{n}", {}).get("a"), cells.get(f"{family}_{n}", {}).get("b")
            if a and b:
                p50 = b["wall_s"]["p50"] / a["wall_s"]["p50"]
                p95 = b["wall_s"]["p95"] / a["wall_s"]["p95"]
                row: dict[str, Any] = {"p50_ratio": p50, "p95_ratio": p95}
                if family == "oracle":
                    row["p95_directional_ok"] = p95 <= 1.15
                    row["pass"] = p50 <= 1.10
                else:
                    row["pass"] = p50 <= 1.10 and p95 <= 1.15
                bars[f"W_{family}_{n}"] = row
    one, base = inc("native_10M_onegroup", "b", "max"), inc("native_10M", "b", "p95")
    if one is not None and base is not None:
        bars["R"] = {"value": one, "limit": base + 128 * MIB, "pass": one <= base + 128 * MIB}
    for cell, per in cells.items():
        for config, c in per.items():
            if config == "b":
                ok = c["lifetime_peak_bytes"]["max"] <= CEILING
                bars[f"ceiling_{cell}"] = {"value": c["lifetime_peak_bytes"]["max"], "pass": ok}
    out["all_pass"] = not out["problems"] and all(b["pass"] for b in bars.values())
    print(json.dumps(out, indent=1))
    return 0 if out["all_pass"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--cell", choices=sorted(CELLS), required=True)
    runner.add_argument("--python", required=True)
    runner.add_argument("--out-dir", required=True)
    runner.add_argument("--work-root", required=True)
    runner.set_defaults(func=run)
    summer = sub.add_parser("summarize")
    summer.add_argument("--out-dir", required=True)
    summer.set_defaults(func=summarize)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
