"""B2 merge benchmark: auto-chunk on the chunked dispatcher, speed and memory
(plan 2026-10-01-dispatcher-auto-chunk, Design 11). Merge evidence, not the Phase E
100M-row gate.

Run it under the box-wide test lock, one process at a time, from the worktree:

    flock /home/cam/.cache/pytest-one.lock \\
        PYTHONPATH=src <python> scripts/bench-auto-chunk/bench_auto_chunk.py --out run.json

(`flock` wraps the whole driver; the driver spawns its workers one at a time and
takes no lock of its own.) Use the companion venv: the speed bars are about the
native route, and configurations b, c and e must report `native_admitted`.

Configurations (each is `variant, dispatcher, native_threads`):
  a    base, off, 1
  b    base, on, 1
  c    base, on, 4
  d0   extra, off, 1      d1   extra, on, 1     (an unconfigured column: B1 reroutes)
  e0   pandas, off, 1     e1   pandas, on, 1    (a pandas-origin source with schema metadata)

Method: one discarded warmup trial per configuration, then `--rounds` (7) measured
rounds; each round runs every configuration once in an order shuffled by a seeded RNG
(seed and orders recorded). Every trial is a fresh subprocess. Each dispatcher-off
reference output is generated in a separate unmeasured subprocess; this parent does
every comparison, so reference generation, loading and comparison never touch a
measured trial's RSS. Reported per configuration: all wall times, p50, max, and each
trial's peak RSS (whole process, interpreter start and source construction included).

Bars (frozen by the plan, never adjusted here): every trial under 2 GiB
(2 * 1024**3 bytes); b, c and e1 each have lower p50 and lower max-of-seven than their
dispatcher-off run (a for b and c, e0 for e1); d1 p50 at most 1.05 times d0 p50.
Missing a bar is reported, and the exit status is 1.
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
WORKER = HERE / "bench_worker_auto_chunk.py"
DEFAULT_SEED = 20261001
DEFAULT_ROWS = 1_000_000
CEILING_BYTES = 2 * 1024**3
FALLBACK_FACTOR = 1.05

# name -> (variant, dispatcher, native_threads)
CONFIGS: dict[str, tuple[str, str, int]] = {
    "a": ("base", "off", 1),
    "b": ("base", "on", 1),
    "c": ("base", "on", 4),
    "d0": ("extra", "off", 1),
    "d1": ("extra", "on", 1),
    "e0": ("pandas", "off", 1),
    "e1": ("pandas", "on", 1),
}
REFERENCE_OF = {
    "a": "base",
    "b": "base",
    "c": "base",
    "d0": "extra",
    "d1": "extra",
    "e0": "pandas",
    "e1": "pandas",
}
# (candidate, baseline): the candidate must be faster at p50 and at max-of-seven.
SPEED_BARS = (("b", "a"), ("c", "a"), ("e1", "e0"))


def _env() -> dict[str, str]:
    return {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}


def _worker(*args: str) -> None:
    subprocess.run(  # noqa: S603 fixed benchmark worker invocation, no untrusted input
        [sys.executable, str(WORKER), *args], check=True, env=_env()
    )


def _prepare(work: Path, rows: int) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for variant in ("base", "extra", "pandas"):
        path = work / f"source_{variant}.parquet"
        _worker("prepare", "--variant", variant, "--path", str(path), "--rows", str(rows))
        sources[variant] = path
    return sources


def _trial(name: str, sources: dict[str, Path], out_dir: Path, rows: int) -> dict[str, Any]:
    variant, dispatcher, threads = CONFIGS[name]
    _worker(
        "run",
        "--variant", variant,
        "--dispatcher", dispatcher,
        "--threads", str(threads),
        "--source", str(sources[variant]),
        "--out-dir", str(out_dir),
        "--rows", str(rows),
    )  # fmt: skip
    evidence = json.loads((out_dir / "evidence.json").read_text())
    evidence["output_path"] = str(out_dir / "output.arrow")
    return evidence


def _load(path: str) -> pa.Table:
    with pa.OSFile(path, "rb") as source:
        return pa.ipc.open_file(source).read_all()


def _check_trial(name: str, evidence: dict[str, Any], reference: pa.Table) -> list[str]:
    """Guarantee 3 against the configuration's dispatcher-off reference."""
    problems: list[str] = []
    out = _load(evidence["output_path"])
    if not out.equals(reference):
        problems.append("output differs from the dispatcher-off reference")
    if evidence["rss_bytes"] >= CEILING_BYTES:
        problems.append(
            f"peak RSS {evidence['rss_bytes']} is at or over the {CEILING_BYTES} ceiling"
        )
    if CONFIGS[name][1] == "on":
        if evidence["output_schema_metadata"]:
            problems.append(f"schema metadata present: {evidence['output_schema_metadata']}")
        if not out.schema.field("ref").equals(pa.field("ref", pa.string()), check_metadata=True):
            problems.append("passthrough field differs from the source field")
        route = evidence["chunked_route"]
        if name == "d1":
            if route["native_admitted"] or not str(route["reroute_reason"]).startswith(
                "uncovered_columns"
            ):
                problems.append(
                    f"d1 expected an uncovered_columns reroute, got {route['reroute_reason']!r}"
                )
        else:
            oracle = [
                c["column"] for c in route["columns"] if c["executed_backend"] == "pandas_oracle"
            ]
            if not route["native_admitted"] or oracle:
                problems.append(f"expected native_admitted with no oracle columns, got {route}")
        if evidence["auto_chunk"]["lane"] != "dispatcher":
            problems.append("lane is not the dispatcher")
    elif evidence["auto_chunk"]["lane"] != "legacy_oracle":
        problems.append("dispatcher-off trial did not run the legacy lane")
    return problems


def _percentile_50(values: list[float]) -> float:
    return statistics.median(values)


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
    source_nbytes: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="bench-auto-chunk-") as tmp:
        work = Path(tmp)
        sources = _prepare(work, args.rows)
        references: dict[str, pa.Table] = {}
        for variant in ("base", "extra", "pandas"):
            ref = _trial(
                {"base": "a", "extra": "d0", "pandas": "e0"}[variant],
                sources,
                work / f"ref_{variant}",
                args.rows,
            )
            references[variant] = _load(ref["output_path"])
        for name in CONFIGS:  # discarded warmup
            _trial(name, sources, work / f"warmup_{name}", args.rows)
        for round_index in range(args.rounds):
            order = list(CONFIGS)
            rng.shuffle(order)
            orders.append(order)
            for name in order:
                evidence = _trial(name, sources, work / f"r{round_index}_{name}", args.rows)
                wall[name].append(evidence["wall_s"])
                rss[name].append(evidence["rss_bytes"])
                source_nbytes[CONFIGS[name][0]] = evidence["source_nbytes"]
                problems += [
                    f"{name} round {round_index}: {p}"
                    for p in _check_trial(name, evidence, references[REFERENCE_OF[name]])
                ]

    summary = {
        name: {
            "wall_s": wall[name],
            "p50": _percentile_50(wall[name]),
            "max": max(wall[name]),
            "rss_bytes": rss[name],
            "max_rss_bytes": max(rss[name]),
        }
        for name in CONFIGS
    }
    bars: dict[str, bool] = {}
    for cand, base in SPEED_BARS:
        bars[f"{cand}_p50_lt_{base}"] = summary[cand]["p50"] < summary[base]["p50"]
        bars[f"{cand}_max_lt_{base}"] = summary[cand]["max"] < summary[base]["max"]
    bars["d1_p50_le_1.05_d0"] = summary["d1"]["p50"] <= FALLBACK_FACTOR * summary["d0"]["p50"]
    bars["every_trial_under_ceiling"] = all(
        m < CEILING_BYTES for name in CONFIGS for m in rss[name]
    )
    bars["correctness"] = not problems
    result = {
        "rows": args.rows,
        "rounds": args.rounds,
        "seed": args.seed,
        "orders": orders,
        "ceiling_bytes": CEILING_BYTES,
        "source_nbytes": source_nbytes,
        "versions": _versions(),
        "configs": {
            name: {"variant": v, "dispatcher": d, "native_threads": t}
            for name, (v, d, t) in CONFIGS.items()
        },
        "summary": summary,
        "bars": bars,
        "problems": problems,
    }
    Path(args.out).write_text(json.dumps(result, indent=1, allow_nan=False))
    for name in CONFIGS:
        s = summary[name]
        print(
            f"{name:3s} p50={s['p50']:.3f}s max={s['max']:.3f}s maxRSS={s['max_rss_bytes'] / 1024**3:.3f} GiB walls={[round(w, 3) for w in s['wall_s']]}"
        )
    for bar, ok in bars.items():
        print(f"{'PASS' if ok else 'FAIL'} {bar}")
    for problem in problems:
        print("PROBLEM", problem)
    return 0 if all(bars.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
