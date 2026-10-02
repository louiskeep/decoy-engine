"""B8 merge benchmark: native admission of unconfigured passthrough columns, speed and
memory (plan 2026-10-01-native-unconfigured-passthrough, Design 11). Merge evidence, not
the Phase E 100M-row gate.

Run it under the box-wide test lock, one process at a time, from the worktree, with the
companion venv (the bars are about the native route, and b1, b4 and d_after must report
`native_admitted`):

    flock /home/cam/.cache/pytest-one.lock \\
        python scripts/bench-unconfigured-passthrough/bench_unconfigured_passthrough.py \\
        --before-src <path to a src/ tree at the base commit> --out run.json

`--before-src` is the `src` directory of the code before B8 (for example from
`git archive <base-commit> src`); the after side is this worktree's `src`.

Configurations (each is `src side, unconfigured columns, native_threads`):
  a1 before, 8, 1     a4 before, 8, 4     (oracle route, `uncovered_columns` reroute)
  b1 after,  8, 1     b4 after,  8, 4     (native route)
  d_before before, 0, 1    d_after after, 0, 1   (the four configured columns only)
  e  after, 8 with `unconfigured_column_policy: error`: correctness only, not timed.

Method: one discarded warmup round, then `--rounds` (7) measured rounds; each round runs
every timed configuration once in an order shuffled by a seeded RNG (seed and orders
recorded). Every trial is a fresh subprocess. The references (a1's output for the N = 8
configurations, d_before's for the N = 0 ones) come from separate unmeasured subprocesses;
this parent does every comparison, so reference generation, loading and comparison never
touch a measured trial's RSS. Reported per configuration: all wall times, p50, max, and
each trial's peak RSS through iterator drain.

Bars (frozen by the plan, never adjusted here): b1 has lower p50 and lower max-of-seven
than a1, b4 than a4; d_after p50 is at most 1.05 times d_before p50; every b1, b4 and
d_after trial stays under 2 GiB (2 * 1024**3 bytes). Missing a bar is reported and the exit
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
WORKER = HERE / "bench_worker_unconfigured_passthrough.py"
AFTER_SRC = HERE.parents[1] / "src"
DEFAULT_SEED = 20261001
DEFAULT_ROWS = 1_000_000
CEILING_BYTES = 2 * 1024**3
FALLBACK_FACTOR = 1.05
CHUNK_ROWS = 50_000

# name -> (side, unconfigured columns, native_threads)
CONFIGS: dict[str, tuple[str, int, int]] = {
    "a1": ("before", 8, 1),
    "a4": ("before", 8, 4),
    "b1": ("after", 8, 1),
    "b4": ("after", 8, 4),
    "d_before": ("before", 0, 1),
    "d_after": ("after", 0, 1),
}
REFERENCE_OF = {
    "a1": "a1",
    "a4": "a1",
    "b1": "a1",
    "b4": "a1",
    "d_before": "d_before",
    "d_after": "d_before",
}
CEILING_CONFIGS = ("b1", "b4", "d_after")
SPEED_BARS = (("b1", "a1"), ("b4", "a4"))


def _env(side: str, before_src: Path) -> dict[str, str]:
    src = before_src if side == "before" else AFTER_SRC
    return {
        **os.environ,
        "PYTHONPATH": str(src),
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }


def _trial(
    name: str, before_src: Path, out_dir: Path, rows: int, *, policy: str = "warn"
) -> dict[str, Any]:
    side, unconfigured, threads = CONFIGS.get(name, ("after", 8, 1))
    subprocess.run(  # noqa: S603 fixed benchmark worker invocation, no untrusted input
        [
            sys.executable,
            str(WORKER),
            "run",
            "--unconfigured",
            str(unconfigured),
            "--policy",
            policy,
            "--threads",
            str(threads),
            "--out-dir",
            str(out_dir),
            "--rows",
            str(rows),
        ],
        check=True,
        env=_env(side, before_src),
    )
    evidence = json.loads((out_dir / "evidence.json").read_text())
    evidence["output_path"] = str(out_dir / "output.arrow")
    return evidence


def _load(path: str) -> pa.Table:
    with pa.OSFile(path, "rb") as source:
        return pa.ipc.open_file(source).read_all()


def _check_trial(
    name: str, evidence: dict[str, Any], reference: dict[str, Any], rows: int
) -> list[str]:
    problems: list[str] = []
    if not _load(evidence["output_path"]).equals(
        _load(reference["output_path"]), check_metadata=True
    ):
        problems.append("output differs from the reference (check_metadata=True)")
    if evidence["warnings"] != reference["warnings"]:
        problems.append("per-chunk warnings differ from the reference")
    expected_chunks = -(-rows // CHUNK_ROWS)
    if evidence["chunks"] != expected_chunks or evidence["error"] is not None:
        problems.append(f"expected {expected_chunks} chunks and no error, got {evidence['chunks']}")
    side, unconfigured, _threads = CONFIGS[name]
    native = side == "after" or unconfigured == 0
    if evidence["native_admitted"] is not native:
        problems.append(f"native_admitted is {evidence['native_admitted']}, expected {native}")
    if native and evidence["reroute_reason"] is not None:
        problems.append(f"unexpected reroute reason {evidence['reroute_reason']!r}")
    if not native and not str(evidence["reroute_reason"]).startswith("uncovered_columns"):
        problems.append(
            f"expected an uncovered_columns reroute, got {evidence['reroute_reason']!r}"
        )
    if name in CEILING_CONFIGS and evidence["rss_bytes"] >= CEILING_BYTES:
        problems.append(
            f"peak RSS {evidence['rss_bytes']} is at or over the {CEILING_BYTES} ceiling"
        )
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
        "python": platform.python_version(),
        "pyarrow": pa.__version__,
        "pandas": pandas.__version__,
        "engine": getattr(decoy_engine, "__version__", "unknown"),
        "companion": companion,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", required=True, help="JSON result path")
    parser.add_argument("--before-src", required=True, help="src/ tree of the pre-B8 code")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    before_src = Path(args.before_src).resolve()

    rng = random.Random(args.seed)
    wall: dict[str, list[float]] = {name: [] for name in CONFIGS}
    rss: dict[str, list[int]] = {name: [] for name in CONFIGS}
    problems: list[str] = []
    orders: list[list[str]] = []
    source_nbytes: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="bench-unconfigured-") as tmp:
        work = Path(tmp)
        references = {
            name: _trial(name, before_src, work / f"ref_{name}", args.rows)
            for name in ("a1", "d_before")
        }
        for name in CONFIGS:  # discarded warmup round
            _trial(name, before_src, work / f"warmup_{name}", args.rows)
        for round_index in range(args.rounds):
            order = list(CONFIGS)
            rng.shuffle(order)
            orders.append(order)
            for name in order:
                evidence = _trial(name, before_src, work / f"r{round_index}_{name}", args.rows)
                wall[name].append(evidence["wall_s"])
                rss[name].append(evidence["rss_bytes"])
                source_nbytes[f"{CONFIGS[name][0]}_{CONFIGS[name][1]}"] = evidence["source_nbytes"]
                problems += [
                    f"{name} round {round_index}: {p}"
                    for p in _check_trial(name, evidence, references[REFERENCE_OF[name]], args.rows)
                ]
        error_run = _trial("e", before_src, work / "error_policy", args.rows, policy="error")
        error_ok = (
            error_run["error"] is not None
            and error_run["error"]["code"] == "undeclared_output_columns"
            and error_run["native_admitted"] is False
            and str(error_run["reroute_reason"]).startswith("uncovered_columns")
            and error_run["chunks"] == 0
        )
        if not error_ok:
            problems.append(f"policy error did not raise on the oracle route: {error_run}")

    summary = {
        name: {
            "wall_s": wall[name],
            "p50": statistics.median(wall[name]),
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
    bars["d_after_p50_le_1.05_d_before"] = (
        summary["d_after"]["p50"] <= FALLBACK_FACTOR * summary["d_before"]["p50"]
    )
    bars["every_ceiling_trial_under_2GiB"] = all(
        m < CEILING_BYTES for name in CEILING_CONFIGS for m in rss[name]
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
            name: {"side": s, "unconfigured_columns": n, "native_threads": t}
            for name, (s, n, t) in CONFIGS.items()
        },
        "summary": summary,
        "bars": bars,
        "problems": problems,
    }
    Path(args.out).write_text(json.dumps(result, indent=1, allow_nan=False))
    for name in CONFIGS:
        s = summary[name]
        gib = s["max_rss_bytes"] / 1024**3
        print(
            f"{name:9s} p50={s['p50']:.3f}s max={s['max']:.3f}s maxRSS={gib:.3f} GiB "
            f"walls={[round(w, 3) for w in s['wall_s']]}"
        )
    for bar, ok in bars.items():
        print(f"{'PASS' if ok else 'FAIL'} {bar}")
    for problem in problems:
        print("PROBLEM", problem)
    return 0 if all(bars.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
