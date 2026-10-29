#!/usr/bin/env python3
"""C6c-ii perf matrix for the text_redact Rust span kernel (plan 7.7 / completion-plan section 4.1).

Three paired measurements on an IDENTICAL fixture, same host, release build, each in its own
subprocess so peak RSS (`ru_maxrss`) is isolated per path:

  baseline8   the SHIPPED path: the eight lookaround-free detectors run in the compiled kernel,
              while `ssn`/`us_zip`/`street_address` fall back to the per-cell Python scan. This is
              what the lookaround port must not regress against -- NOT full Python. Reproduced from
              the same build by narrowing the Rust-supported set to the eight (so the three
              lookaround ids route to `merge_text_redact_spans`'s Python `finditer`), exactly the
              pre-port routing.
  new11       the lookaround-port path: all eleven detectors run in the compiled kernel.
  python      full Python (`native_text_redact` with the loader forced to `None`). REPORTED ONLY,
              never gated -- it is the full-Python headline, not the acceptance baseline.

The acceptance guard is new11-vs-baseline8 (the comparison that catches a regression against what
ships). Each measurement records median, mean, run-to-run stdev (NOT per-cell variance) and an
observed tail (max/median), over >= DEFAULT_REPS reps after 1 warmup; a handful of reps cannot
support a reliable p95/p99, so an observed max/median is reported instead (plan 4.1).

Row count: 200,000 by default. The plan sanctions 200k with >= 5 reps (over 1M with 3) because a
credible stdev/tail needs reps, and per-cell throughput extrapolates linearly to 1M (x5) on these
ASCII-dominant fixtures. Pass `--rows 1000000 --reps 10` to measure 1M directly where time allows.

Workload matrix (each row = a pinned fixture):
  R-representative      clinical-notes prose, ASCII-dominant with a ~5% non-ASCII fraction, default
                        all-11-detector config. The headline row. Note length ~ 20-400 chars.
  R-all-ineligible      every cell carries a non-ASCII char or a 0x1c-0x1f separator (all route to
                        Python); default config. Worst-case routing overhead.
  R-python-only         `detectors` = the three lookaround ids only. baseline8 runs them in Python;
                        new11 runs them in Rust -- the clearest view of the port's own win.
  R-short-no-match      short no-PII cells (1-20 chars), default config. Per-cell fixed overhead.

Pass/fail (executable, every row), applied to new11 vs baseline8:
  latency floor    median_new11 <= 1.05 * median_baseline8  (no more than 5% regression; the port
                   removes the per-cell Python lookaround scan, so new11 should be faster)
  peak-RSS budget  peak_rss_new11 <= 1.20 * peak_rss_baseline8
Speedups are REPORTED: new11-over-baseline8 (the port's win over the shipped path) and
new11-over-python (the full-Python headline). Fallback is detected by execution assertion
(tests 7.6), never by timing.

Before timing, the driver asserts baseline8, new11 and full Python produce BYTE-IDENTICAL spliced
output on a small sample of the representative fixture, so the three paths are measured doing the
same work (a broken baseline reproduction would otherwise flatter the comparison).

Usage:
  python scripts/bench_text_redact.py [--rows N] [--reps R]        # driver (spawns workers)
  python scripts/bench_text_redact.py --worker WHICH PATH ROWS REPS SEED  # one measurement
  python scripts/bench_text_redact.py --equivalence-check              # byte-parity sample only
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import statistics
import subprocess
import sys
import time

# Run from the repo tree even under a venv whose editable `decoy_engine` points at
# another checkout (the companion venv's `.pth` targets the main checkout, not this
# worktree). Without this a standalone `python scripts/bench_text_redact.py` imports
# the wrong `decoy_engine`; under pytest the pyproject pythonpath already handles it.
_REPO_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_REPO_SRC) and _REPO_SRC not in sys.path:
    sys.path.insert(0, _REPO_SRC)

import pyarrow as pa  # noqa: E402 -- after the sys.path shim above

LATENCY_FLOOR = 1.05
RSS_BUDGET = 1.20
DEFAULT_ROWS = 200_000
DEFAULT_REPS = 10

# The eight lookaround-free detectors: the Rust-supported set of the SHIPPED (pre-port) kernel.
# The baseline8 worker narrows the live catalog to these so the three lookaround detectors route to
# the Python `finditer` path, reproducing the shipped routing from the current compiled build.
_SHIPPED_EIGHT = ("email", "us_phone", "pan", "iban", "ipv4", "icd10", "npi", "url")

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


def _install_shipped_eight_kernel() -> None:
    """Reproduce the SHIPPED 8-detector kernel on the current build: narrow the Rust-supported set
    to the eight lookaround-free ids so `ssn`/`us_zip`/`street_address` route to Python, and return
    the real compiled kernel directly (bypassing the loader's supported-set check, which would now
    see the deliberate 8-vs-11 mismatch and fall back to full Python). Detection stays byte-
    identical -- it is the same eleven detectors, just the pre-port routing of three of them."""
    from decoy_engine_native import _kernel as compiled

    from decoy_engine.execution.native import _kernels_scalar
    from decoy_engine.execution.native import _text_redact_kernel as trk

    trk.SUPPORTED_IDS = _SHIPPED_EIGHT
    trk._SUPPORTED_SET = frozenset(_SHIPPED_EIGHT)
    trk._INDEX_OF = {det_id: i for i, det_id in enumerate(_SHIPPED_EIGHT)}
    shipped_kernel = trk._CompiledTextRedactKernel(compiled.text_redact_candidates)
    _kernels_scalar.load_text_redact_kernel = lambda: shipped_kernel


def _redact(array: pa.Array, detectors: tuple[str, ...] | None) -> list[str | None]:
    from decoy_engine.execution.native._kernels_scalar import native_text_redact

    token = "[REDACTED]"  # noqa: S105 - redaction placeholder, not a credential
    return native_text_redact(
        array, detectors=detectors, token=token, label_token=False
    ).to_pylist()


def run_worker(which: str, path: str, rows: int, reps: int, seed: int) -> None:
    from decoy_engine.execution.native import _kernels_scalar
    from decoy_engine.execution.native._text_redact_kernel import load_text_redact_kernel

    array, detectors = build_fixture(path, rows, seed)
    hit_rate = _ascii_hit_rate(array)

    if which == "python":
        _kernels_scalar.load_text_redact_kernel = lambda: None  # force the full Python path
    elif which == "baseline8":
        _install_shipped_eight_kernel()
    elif which == "new11":
        if load_text_redact_kernel() is None:
            raise SystemExit("new11 path requires the compiled companion; build it first")
    else:
        raise SystemExit(f"unknown worker {which!r}")

    def once() -> None:
        _redact(array, detectors)

    once()  # warmup
    samples = []
    for _ in range(reps):
        t = time.perf_counter()
        once()
        samples.append(time.perf_counter() - t)
    median = statistics.median(samples)
    print(
        json.dumps(
            {
                "which": which,
                "path": path,
                "median_s": median,
                "mean_s": statistics.fmean(samples),
                "min_s": min(samples),
                "max_s": max(samples),
                # Run-to-run (NOT per-cell) dispersion: stdev over reps, and an observed tail as
                # max/median (a reliable p95/p99 needs far more than a handful of reps).
                "stdev_s": statistics.stdev(samples) if len(samples) > 1 else 0.0,
                "rel_stdev": (statistics.stdev(samples) / median)
                if (len(samples) > 1 and median)
                else 0.0,
                "tail_max_over_median": (max(samples) / median) if median else float("inf"),
                "reps": reps,
                "rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "hit_rate": hit_rate,
            }
        )
    )


_EQUIV_SAMPLE_ROWS = 3000


def _equiv_hash(which: str, seed: int) -> str:
    """The sha256 of one path's spliced output over a small representative + lookaround-only sample.
    baseline8, new11 and full Python must hash identically (same detection, different routing)."""
    import hashlib

    from decoy_engine.execution.native import _kernels_scalar

    if which == "python":
        _kernels_scalar.load_text_redact_kernel = lambda: None
    elif which == "baseline8":
        _install_shipped_eight_kernel()
    # new11: leave the real loader in place.

    array, detectors = build_fixture("R-representative", _EQUIV_SAMPLE_ROWS, seed)
    array_lk, detectors_lk = build_fixture("R-python-only", _EQUIV_SAMPLE_ROWS, seed)
    out = _redact(array, detectors) + _redact(array_lk, detectors_lk)
    h = hashlib.sha256()
    for cell in out:
        h.update(b"\x00" if cell is None else cell.encode("utf-8", "surrogatepass"))
        h.update(b"\x01")
    return h.hexdigest()


def _equivalence_check(seed: int) -> None:
    """Spawn baseline8/new11/python each in its own process (clean import state) and assert their
    output hashes agree, so the three measured paths are doing byte-identical work."""
    hashes = {}
    for which in ("new11", "baseline8", "python"):
        out = subprocess.run(  # noqa: S603 - fixed argv (self re-invocation), no shell
            [sys.executable, __file__, "--equiv-worker", which, str(seed)],
            capture_output=True,
            text=True,
            check=True,
        )
        hashes[which] = out.stdout.strip().splitlines()[-1]
    if len({*hashes.values()}) != 1:
        raise SystemExit(
            f"EQUIVALENCE FAILURE: baseline8/new11/python do not redact identically {hashes} -- "
            "the baseline reproduction or the kernel diverged; the perf comparison would be invalid."
        )
    print(
        f"equivalence OK: baseline8 == new11 == python ({hashes['new11'][:12]}...) on "
        f"{_EQUIV_SAMPLE_ROWS} representative + {_EQUIV_SAMPLE_ROWS} lookaround-only cells"
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
    ap.add_argument("--equiv-worker", nargs=2, metavar=("WHICH", "SEED"))
    ap.add_argument("--equivalence-check", action="store_true")
    args = ap.parse_args()

    seed = 20261009

    if args.worker:
        which, path, rows_s, reps_s, seed_s = args.worker
        run_worker(which, path, int(rows_s), int(reps_s), int(seed_s))
        return 0

    if args.equiv_worker:
        which, seed_s = args.equiv_worker
        print(_equiv_hash(which, int(seed_s)))
        return 0

    if args.equivalence_check:
        _equivalence_check(seed)
        return 0

    rows_list = (
        "R-representative",
        "R-all-ineligible",
        "R-python-only",
        "R-short-no-match",
    )
    print(
        f"text_redact perf matrix: {args.rows:,} rows, {args.reps} reps + 1 warmup, release build, "
        f"paired subprocesses (baseline8 = shipped 8-detector kernel; new11 = lookaround port)\n"
    )

    # Byte-parity sanity (its own subprocess so a reload cannot taint the timed runs).
    eq = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, __file__, "--equivalence-check"],
        capture_output=True,
        text=True,
        check=True,
    )
    print(eq.stdout.strip() + "\n")

    header = (
        f"{'row':<18}{'base8 med':>11}{'new11 med':>11}{'x vs base8':>11}{'x vs py':>10}"
        f"{'lat<=1.05':>11}{'rss<=1.20':>11}{'new stdev':>11}{'new tail':>10}{'hit%':>7}"
    )
    print(header)
    print("-" * len(header))
    failures = []
    detail: list[dict] = []
    for path in rows_list:
        base = _spawn("baseline8", path, args.rows, args.reps, seed)
        new = _spawn("new11", path, args.rows, args.reps, seed)
        py = _spawn("python", path, args.rows, args.reps, seed)
        detail.append({"path": path, "baseline8": base, "new11": new, "python": py})

        speed_base = base["median_s"] / new["median_s"] if new["median_s"] else float("inf")
        speed_py = py["median_s"] / new["median_s"] if new["median_s"] else float("inf")
        lat_ratio = new["median_s"] / base["median_s"] if base["median_s"] else float("inf")
        rss_ratio = new["rss_kb"] / base["rss_kb"] if base["rss_kb"] else float("inf")
        lat_ok = lat_ratio <= LATENCY_FLOOR
        rss_ok = rss_ratio <= RSS_BUDGET
        if not lat_ok:
            failures.append(
                f"{path}: latency {lat_ratio:.3f}x > {LATENCY_FLOOR} (new11 vs baseline8)"
            )
        if not rss_ok:
            failures.append(
                f"{path}: peak RSS {rss_ratio:.3f}x > {RSS_BUDGET} (new11 vs baseline8)"
            )
        print(
            f"{path:<18}{base['median_s']:>11.4f}{new['median_s']:>11.4f}{speed_base:>10.2f}x"
            f"{speed_py:>9.2f}x{('OK' if lat_ok else 'FAIL'):>11}{('OK' if rss_ok else 'FAIL'):>11}"
            f"{new['rel_stdev'] * 100:>10.1f}%{new['tail_max_over_median']:>9.2f}x"
            f"{new['hit_rate'] * 100:>6.1f}%"
        )
    print()
    print("variance is RUN-TO-RUN (stdev / median over reps), not per-cell; tail = max/median.")
    print("'x vs base8' is the port's win over the shipped path (gated); 'x vs py' is the full-")
    print("Python headline (reported only). Fallback is proven by execution tests, not timing.")
    print()
    print(json.dumps({"rows": args.rows, "reps": args.reps, "rows_detail": detail}, indent=2))
    print()
    if failures:
        print("GATE FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("all rows within the latency floor and RSS budget (new11 vs baseline8)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
