"""B7 merge benchmark: independent multi-table dispatch, speed and memory
(plan 2026-10-01-multi-table-dispatch, section 11). Merge evidence, not the Phase E
100M-row gate.

Run it under the box-wide test lock, one process at a time, from the worktree:

    flock /home/cam/.cache/pytest-one.lock \\
        PYTHONPATH=src <python> scripts/bench-multi-table/bench_multi_table.py --out run.json

(`flock` wraps the whole driver; the driver spawns its workers one at a time and takes
no lock of its own.) Use the companion venv: the speed bars are about the native route,
and configurations b and c must report `native_admitted` for `t1` and `t2`.

Configurations (each is `variant, split, native_threads`):
  a    base, off, 1     (today's single full-frame call)
  b    base, on, 1
  c    base, on, 4
  d0   extra, off, 1    d1   extra, on, 1     (an unconfigured column on t1 and t2: B1
                                               reroutes both to its oracle route; B8 is
                                               not on main, so this trigger applies)

Method: one discarded warmup trial per configuration, then `--rounds` (7) measured rounds;
each round runs every configuration once in an order shuffled by a seeded RNG (seed and
orders recorded). Every trial is a fresh subprocess. Reference outputs come from separate
unmeasured subprocesses (split off for every variant, split on for b, c and d1); this
parent does every comparison. Reported per configuration: all wall times, p50, max, and
each trial's peak RSS (whole process). A worker that only builds the sources reports the
RSS floor common to every trial.

Frozen bars (plan section 11, never adjusted here): every trial under 4 GiB
(4 * 1024**3 bytes); b and c have lower p50 and lower max-of-seven wall time than a, and
p50 and max-of-seven peak RSS no higher than a; d1 p50 wall at most 1.10 times d0 p50, and
d1 p50 and max-of-seven peak RSS no higher than d0. Missing a bar is reported and the exit
status is 1.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pyarrow as pa

HERE = Path(__file__).resolve().parent
WORKER = HERE / "bench_worker_multi_table.py"
DEFAULT_SEED = 20261001
DEFAULT_ROWS = 1_000_000
CEILING_BYTES = 4 * 1024**3
FALLBACK_FACTOR = 1.10

# name -> (variant, split, native_threads)
CONFIGS: dict[str, tuple[str, str, int]] = {
    "a": ("base", "off", 1),
    "b": ("base", "on", 1),
    "c": ("base", "on", 4),
    "d0": ("extra", "off", 1),
    "d1": ("extra", "on", 1),
}
SPLIT_ON = tuple(n for n, (_, s, _) in CONFIGS.items() if s == "on")
TABLES = ("t1", "t2", "t3")


def _env() -> dict[str, str]:
    return {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}


def _worker(*args: str) -> str:
    done = subprocess.run(  # noqa: S603 fixed benchmark worker invocation, no untrusted input
        [sys.executable, str(WORKER), *args],
        check=True,
        env=_env(),
        capture_output=True,
        text=True,
    )
    return done.stdout


def _trial(name: str, source_dirs: dict[str, Path], out_dir: Path, rows: int) -> dict[str, Any]:
    variant, split, threads = CONFIGS[name]
    _worker(
        "run",
        "--variant", variant,
        "--split", split,
        "--threads", str(threads),
        "--source-dir", str(source_dirs[variant]),
        "--out-dir", str(out_dir),
        "--rows", str(rows),
    )  # fmt: skip
    evidence = json.loads((out_dir / "evidence.json").read_text())
    evidence["output_dir"] = str(out_dir)
    return evidence


def _load(out_dir: str, table: str) -> pa.Table:
    with pa.OSFile(str(Path(out_dir) / f"{table}.arrow"), "rb") as source:
        return pa.ipc.open_file(source).read_all()


def _check_trial(
    name: str,
    evidence: dict[str, Any],
    off_reference: dict[str, str],
    on_reference: dict[str, str] | None,
) -> list[str]:
    """Plan section 11 correctness for one measured trial."""
    problems: list[str] = []
    out = {t: _load(evidence["output_dir"], t) for t in TABLES}
    off = {t: _load(off_reference["output_dir"], t) for t in TABLES}
    off_names = json.loads((Path(off_reference["output_dir"]) / "evidence.json").read_text())[
        "output_names"
    ]
    if evidence["rss_bytes"] >= CEILING_BYTES:
        problems.append(
            f"peak RSS {evidence['rss_bytes']} is at or over the {CEILING_BYTES} ceiling"
        )
    if evidence["output_names"] != off_names:
        problems.append(f"output table order {evidence['output_names']} differs from {off_names}")
    if not out["t3"].equals(off["t3"], check_metadata=True):
        problems.append("t3 differs from the split-off reference (check_metadata=True)")
    if CONFIGS[name][1] == "off":
        return problems
    source_field = pa.field("ref", pa.string())
    for table in ("t1", "t2"):
        if not out[table].equals(off[table]):
            problems.append(f"{table} differs from the split-off reference")
        if out[table].schema.metadata is not None:
            problems.append(f"{table} carries schema metadata")
        if not out[table].schema.field("ref").equals(source_field, check_metadata=True):
            problems.append(f"{table} passthrough field differs from the source field")
        if on_reference is not None:
            ref = _load(on_reference["output_dir"], table)
            if not out[table].equals(ref, check_metadata=True):
                problems.append(
                    f"{table} differs from the split-on reference (check_metadata=True)"
                )
    entries = {t["table"]: t for t in evidence["auto_chunk"]["tables"]}
    if [entries[t]["dispatched"] for t in TABLES] != [True, True, False]:
        problems.append(f"dispatch is not t1, t2 dispatched and t3 in the group: {entries}")
    routes = evidence["chunked_route_by_table"]
    for table in ("t1", "t2"):
        route = routes[table]
        if name == "d1":
            if route["native_admitted"] or not str(route["reroute_reason"]).startswith(
                "uncovered_columns"
            ):
                problems.append(f"{table} expected an uncovered_columns reroute, got {route}")
        else:
            oracle = [
                c["column"] for c in route["columns"] if c["executed_backend"] == "pandas_oracle"
            ]
            if not route["native_admitted"] or oracle:
                problems.append(f"{table} expected native_admitted with no oracle columns: {route}")
    return problems


def _versions() -> dict[str, str]:
    import pandas

    import decoy_engine

    try:
        import decoy_engine_native

        companion = getattr(decoy_engine_native, "__version__", "present")
    except ImportError:
        companion = "absent"
    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        platform.processor(),
    )
    return {
        "cpu": cpu,
        "cpu_count": str(os.cpu_count()),
        "python": platform.python_version(),
        "pyarrow": pa.__version__,
        "pandas": pandas.__version__,
        "engine": getattr(decoy_engine, "__version__", "unknown"),
        "companion": companion,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", required=True, help="JSON result path")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    wall: dict[str, list[float]] = {name: [] for name in CONFIGS}
    rss: dict[str, list[int]] = {name: [] for name in CONFIGS}
    problems: list[str] = []
    orders: list[list[str]] = []
    source_nbytes: dict[str, dict[str, int]] = {}
    floors: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="bench-multi-table-") as tmp:
        work = Path(tmp)
        source_dirs = {v: work / f"source_{v}" for v in ("base", "extra")}
        for variant, path in source_dirs.items():
            _worker("prepare", "--variant", variant, "--dir", str(path), "--rows", str(args.rows))
            floors[variant] = json.loads(
                _worker("baseline", "--variant", variant, "--rows", str(args.rows))
            )
        off_refs = {
            "base": _trial("a", source_dirs, work / "ref_off_base", args.rows),
            "extra": _trial("d0", source_dirs, work / "ref_off_extra", args.rows),
        }
        on_refs = {n: _trial(n, source_dirs, work / f"ref_on_{n}", args.rows) for n in SPLIT_ON}
        for name in CONFIGS:  # discarded warmup
            _trial(name, source_dirs, work / f"warmup_{name}", args.rows)
        for round_index in range(args.rounds):
            order = list(CONFIGS)
            rng.shuffle(order)
            orders.append(order)
            for name in order:
                evidence = _trial(name, source_dirs, work / f"r{round_index}_{name}", args.rows)
                wall[name].append(evidence["wall_s"])
                rss[name].append(evidence["rss_bytes"])
                source_nbytes[CONFIGS[name][0]] = evidence["source_nbytes"]
                problems += [
                    f"{name} round {round_index}: {p}"
                    for p in _check_trial(
                        name,
                        evidence,
                        off_refs[CONFIGS[name][0]],
                        on_refs.get(name),
                    )
                ]

    summary = {
        name: {
            "wall_s": wall[name],
            "p50": statistics.median(wall[name]),
            "max": max(wall[name]),
            "rss_bytes": rss[name],
            "p50_rss_bytes": statistics.median(rss[name]),
            "max_rss_bytes": max(rss[name]),
        }
        for name in CONFIGS
    }
    bars: dict[str, bool] = {}
    for cand in ("b", "c"):
        bars[f"{cand}_p50_wall_lt_a"] = summary[cand]["p50"] < summary["a"]["p50"]
        bars[f"{cand}_max_wall_lt_a"] = summary[cand]["max"] < summary["a"]["max"]
        bars[f"{cand}_p50_rss_le_a"] = (
            summary[cand]["p50_rss_bytes"] <= summary["a"]["p50_rss_bytes"]
        )
        bars[f"{cand}_max_rss_le_a"] = (
            summary[cand]["max_rss_bytes"] <= summary["a"]["max_rss_bytes"]
        )
    bars["d1_p50_wall_le_1.10_d0"] = summary["d1"]["p50"] <= FALLBACK_FACTOR * summary["d0"]["p50"]
    bars["d1_p50_rss_le_d0"] = summary["d1"]["p50_rss_bytes"] <= summary["d0"]["p50_rss_bytes"]
    bars["d1_max_rss_le_d0"] = summary["d1"]["max_rss_bytes"] <= summary["d0"]["max_rss_bytes"]
    bars["every_trial_under_ceiling"] = all(m < CEILING_BYTES for n in CONFIGS for m in rss[n])
    bars["correctness"] = not problems
    result = {
        "rows": args.rows,
        "rounds": args.rounds,
        "seed": args.seed,
        "orders": orders,
        "ceiling_bytes": CEILING_BYTES,
        "fallback_factor": FALLBACK_FACTOR,
        "source_nbytes": source_nbytes,
        "source_build_floor": floors,
        "versions": _versions(),
        "configs": {
            name: {"variant": v, "split": s, "native_threads": t}
            for name, (v, s, t) in CONFIGS.items()
        },
        "summary": summary,
        "bars": bars,
        "problems": problems,
    }
    Path(args.out).write_text(json.dumps(result, indent=1, allow_nan=False))
    gib = 1024**3
    for name in CONFIGS:
        s = summary[name]
        print(
            f"{name:3s} p50={s['p50']:.3f}s max={s['max']:.3f}s "
            f"p50RSS={s['p50_rss_bytes'] / gib:.3f} GiB maxRSS={s['max_rss_bytes'] / gib:.3f} GiB "
            f"walls={[round(w, 3) for w in s['wall_s']]}"
        )
    for variant, floor in floors.items():
        print(f"floor {variant}: {floor['rss_bytes'] / gib:.3f} GiB (sources only)")
    for bar, ok in bars.items():
        print(f"{'PASS' if ok else 'FAIL'} {bar}")
    for problem in problems:
        print("PROBLEM", problem)
    return 0 if all(bars.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
