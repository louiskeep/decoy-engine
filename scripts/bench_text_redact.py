#!/usr/bin/env python3
"""C6c-ii perf matrix for the text_redact Rust span kernel (plan 7.7).

Paired baseline-vs-new comparison on an IDENTICAL fixture, same host, release build. "baseline"
is the C6c-i Python path (`native_text_redact` with the loader forced to return `None`); "new" is
the rev-3 path (`native_text_redact` with the compiled kernel). Each (path, row) runs in its own
subprocess so peak RSS (`ru_maxrss`) is isolated per path. For each matrix row we record min/median
latency and the min-max spread over >= 7 reps after 1 warmup, and the measured ASCII-cell hit rate.

Workload matrix (each row = a pinned fixture at 1,000,000 rows):
  R-representative      clinical-notes prose, ASCII-dominant with a ~5% non-ASCII fraction, default
                        all-11-detector config. Note length ~ 20-400 chars (see `_representative`).
  R-all-ineligible      every cell carries a non-ASCII char or a 0x1c-0x1f separator (all route to
                        Python); default config. Worst-case routing overhead.
  R-python-only         `detectors` = the three lookaround ids only (the kernel runs zero supported
                        detectors, but routing + the empty Rust call still execute).
  R-short-no-match      short no-PII cells (1-20 chars), default config. Per-cell fixed overhead.

Pass/fail (executable, every row):
  latency floor    median_new <= 1.05 * median_baseline  (no more than 5% end-to-end regression)
  peak-RSS budget  peak_rss_new <= 1.20 * peak_rss_baseline
Speedup target is REPORTED, not gated (R-representative): the achieved factor + ASCII hit rate are
printed and go in the build record. Fallback is detected by execution assertion (tests 7.6), never
by timing.

Usage:
  python scripts/bench_text_redact.py [--rows N] [--reps R]        # driver (spawns workers)
  python scripts/bench_text_redact.py --worker PATH ROWS REPS SEED # one (path, fixture) measurement
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import statistics
import subprocess
import sys
import time

import pyarrow as pa

LATENCY_FLOOR = 1.05
RSS_BUDGET = 1.20
DEFAULT_ROWS = 1_000_000
DEFAULT_REPS = 7

_PII = [
    "a@b.com",
    "jane.doe+tag@clinic.example.org",
    "(555) 123-4567",
    "212-555-1234",
    "123-45-6789",
    "4111 1111 1111 1111",
    "1234567893",
    "E11.9",
    "z23",
    "10.0.0.1",
    "192.168.1.1",
    "GB82WEST12345698765432",
    "https://example.org/path?q=1",
    "12345",
    "90210-1234",
    "12 Main Street",
]
_FILLERS = [
    "patient",
    "presented",
    "with",
    "complaint",
    "seen",
    "today",
    "follow-up",
    "note",
    "MRN",
    "on",
    "and",
    "the",
    "reports",
    "prescribed",
    "discharged",
    "visit",
    "history",
    "stable",
]


def _representative(rng: random.Random) -> str:
    n = rng.randint(4, 48)  # ~20-400 chars
    words = [rng.choice(_PII) if rng.random() < 0.18 else rng.choice(_FILLERS) for _ in range(n)]
    text = " ".join(words)
    if rng.random() < 0.05:  # ~5% non-ASCII fraction -> Python fallback
        text += " café José"
    return text


def _all_ineligible(rng: random.Random) -> str:
    base = _representative(rng)
    # Force every cell out of the ASCII-safe domain: a non-ASCII char or a 0x1c separator.
    return base + ("\x1c555\x1c" if rng.random() < 0.5 else " café")


def _short_no_match(rng: random.Random) -> str:
    return rng.choice(["ok", "n/a", "stable", "none", "wnl", "see below", "", "x", "fine", "nad"])


def build_fixture(path: str, rows: int, seed: int) -> tuple[pa.Array, tuple[str, ...] | None]:
    rng = random.Random(seed)
    detectors: tuple[str, ...] | None = None
    if path == "R-representative":
        values = [_representative(rng) for _ in range(rows)]
    elif path == "R-all-ineligible":
        values = [_all_ineligible(rng) for _ in range(rows)]
    elif path == "R-python-only":
        values = [_representative(rng) for _ in range(rows)]
        detectors = ("ssn", "us_zip", "street_address")
    elif path == "R-short-no-match":
        values = [_short_no_match(rng) for _ in range(rows)]
    else:
        raise SystemExit(f"unknown matrix row {path!r}")
    return pa.array(values, type=pa.string()), detectors


def _ascii_hit_rate(array: pa.Array) -> float:
    from decoy_engine.execution.native._text_redact_kernel import is_ascii_safe

    cells = array.to_pylist()
    non_null = [c for c in cells if c is not None]
    if not non_null:
        return 0.0
    hits = sum(1 for c in non_null if is_ascii_safe(c))
    return hits / len(non_null)


def run_worker(which: str, path: str, rows: int, reps: int, seed: int) -> None:
    from decoy_engine.execution.native import _kernels_scalar
    from decoy_engine.execution.native._kernels_scalar import native_text_redact
    from decoy_engine.execution.native._text_redact_kernel import load_text_redact_kernel

    array, detectors = build_fixture(path, rows, seed)
    hit_rate = _ascii_hit_rate(array)

    if which == "baseline":
        _kernels_scalar.load_text_redact_kernel = lambda: None  # force the C6c-i Python path
    else:
        if load_text_redact_kernel() is None:
            raise SystemExit("new path requires the compiled companion; build it first")

    def once() -> None:
        native_text_redact(array, detectors=detectors, token="[REDACTED]", label_token=False)  # noqa: S106 - redaction placeholder, not a credential

    once()  # warmup
    samples = []
    for _ in range(reps):
        t = time.perf_counter()
        once()
        samples.append(time.perf_counter() - t)
    print(
        json.dumps(
            {
                "which": which,
                "path": path,
                "median_s": statistics.median(samples),
                "min_s": min(samples),
                "max_s": max(samples),
                "reps": reps,
                "rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "hit_rate": hit_rate,
            }
        )
    )


def _spawn(which: str, path: str, rows: int, reps: int, seed: int) -> dict:
    out = subprocess.run(  # noqa: S603 - fixed argv (this script re-invoking itself), no shell
        [sys.executable, __file__, "--worker", which, path, str(rows), str(reps), str(seed)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    ap.add_argument("--reps", type=int, default=DEFAULT_REPS)
    ap.add_argument("--worker", nargs=5, metavar=("WHICH", "PATH", "ROWS", "REPS", "SEED"))
    args = ap.parse_args()

    if args.worker:
        which, path, rows_s, reps_s, seed_s = args.worker
        run_worker(which, path, int(rows_s), int(reps_s), int(seed_s))
        return 0

    rows_list = (
        "R-representative",
        "R-all-ineligible",
        "R-python-only",
        "R-short-no-match",
    )
    seed = 20261009
    print(
        f"text_redact perf matrix: {args.rows:,} rows, {args.reps} reps + 1 warmup, "
        f"release build, paired subprocesses\n"
    )
    header = f"{'row':<18}{'base med(s)':>12}{'new med(s)':>12}{'speedup':>9}{'lat<=1.05':>11}{'rss<=1.20':>11}{'hit%':>7}"
    print(header)
    print("-" * len(header))
    failures = []
    for path in rows_list:
        base = _spawn("baseline", path, args.rows, args.reps, seed)
        new = _spawn("new", path, args.rows, args.reps, seed)
        speedup = base["median_s"] / new["median_s"] if new["median_s"] else float("inf")
        lat_ratio = new["median_s"] / base["median_s"] if base["median_s"] else float("inf")
        rss_ratio = new["rss_kb"] / base["rss_kb"] if base["rss_kb"] else float("inf")
        lat_ok = lat_ratio <= LATENCY_FLOOR
        rss_ok = rss_ratio <= RSS_BUDGET
        if not lat_ok:
            failures.append(f"{path}: latency {lat_ratio:.3f}x > {LATENCY_FLOOR}")
        if not rss_ok:
            failures.append(f"{path}: peak RSS {rss_ratio:.3f}x > {RSS_BUDGET}")
        print(
            f"{path:<18}{base['median_s']:>12.4f}{new['median_s']:>12.4f}{speedup:>8.2f}x"
            f"{('OK' if lat_ok else 'FAIL'):>11}{('OK' if rss_ok else 'FAIL'):>11}"
            f"{new['hit_rate'] * 100:>6.1f}%"
        )
    print()
    if failures:
        print("GATE FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("all rows within the latency floor and RSS budget")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
