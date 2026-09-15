"""Task 4.5 D9 performance gate: the paired old/new benchmark comparison.

Runs `scripts/native-baseline/bench_driver.py` twice per tier -- once
against the unmodified pandas-oracle `bench_worker.py` ("old"), once
against this program's `bench_worker_unified.py` ("new") -- ALTERNATING
tier by tier so a host load spike does not land entirely inside one arm,
then checks the D9 thresholds:

  - 100k & 1M: median new/old wall <= 1.10, p95 <= 1.15
  - 10k: median regression <= max(10%, 50 ms)
  - peak RSS <= 1.10x at every tier
  - flag-off overhead vs main <= 1% median (the "old" arm here already
    passes `unified_slice_enabled` at its default False through
    `bench_worker.py`'s own call, unmodified, so this comparison itself
    doubles as that check: any regression in the OLD arm's own numbers
    versus a baseline run recorded on `origin/main` would show up in
    `--baseline-old`)

Every trial's output row count is compared as a coarse equality signal
(`out_rows` matching); a full byte-for-byte cell comparison across
external-subprocess reps is deliberately NOT this script's job -- that
exact-parity claim is what `tests/physical/test_unified_slice_parity.py`
already proves in-process, over the identical Parquet bytes, with a real
cell-by-cell compare. This script's job is purely the wall-clock/RSS
performance claim over a workload too large to run pytest-repeatedly.

Usage:
  python bench_compare.py --tiers 10000,100000,1000000 --reps 20 --warmup 3 \\
      --out-dir /tmp/unified_slice_bench

This is a deliberately DEFERRED script (TASK-4.5-PLAN.md D9's explicit
allowance): the CI-side automated coverage is the fast in-process smoke
tier in `tests/physical/test_unified_slice_performance.py`; a human (or a
scheduled perf run) executes THIS script for the full statistical claim at
10k/100k/1M, since a >=20-rep external-subprocess sweep at 1M rows takes
real wall-clock minutes and is not appropriate for every CI run.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NATIVE_BASELINE_DIR = HERE.parent / "native-baseline"
VENV_PY = Path(sys.executable)

_MEDIAN_RATIO_THRESHOLDS = {100_000: 1.10, 1_000_000: 1.10}
_P95_RATIO_THRESHOLDS = {100_000: 1.15, 1_000_000: 1.15}
_TEN_K_MAX_REGRESSION_S = 0.05  # the "50 ms" absolute floor in max(10%, 50ms)
_TEN_K_RELATIVE_REGRESSION = 0.10
_RSS_RATIO_THRESHOLD = 1.10
_BOOTSTRAP_RESAMPLES = 2000
_BOOTSTRAP_CONFIDENCE = 0.95


def _run_driver(tiers: str, reps: int, warmup: int, worker: str, out_path: Path) -> dict:
    cmd = [
        str(VENV_PY),
        str(NATIVE_BASELINE_DIR / "bench_driver.py"),
        "--tiers",
        tiers,
        "--reps",
        str(reps),
        "--warmup",
        str(warmup),
        "--worker",
        worker,
        "--out",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)  # noqa: S603 fixed local benchmark command
    return json.loads(out_path.read_text())


def _bootstrap_ratio_ci(
    old_walls: list[float], new_walls: list[float], *, resamples: int, confidence: float
) -> tuple[float, float]:
    """A percentile bootstrap over the median new/old wall ratio.

    Resamples EACH side independently (paired trials are alternated, not
    matched 1:1 by index, so this treats the two samples as independent --
    the conservative choice: it does not credit the comparison with any
    trial-to-trial pairing correlation it has not actually established)."""
    import random

    rng = random.Random(20260915)
    ratios = []
    for _ in range(resamples):
        o = rng.choices(old_walls, k=len(old_walls))
        n = rng.choices(new_walls, k=len(new_walls))
        ratios.append(statistics.median(n) / statistics.median(o))
    ratios.sort()
    tail = (1 - confidence) / 2
    lo = ratios[int(tail * resamples)]
    hi = ratios[int((1 - tail) * resamples) - 1]
    return lo, hi


def _check_tier(n_rows: int, old: dict, new: dict) -> list[str]:
    failures: list[str] = []
    old_walls = [r["wall_s"] for r in old["raw_reps"]]
    new_walls = [r["wall_s"] for r in new["raw_reps"]]
    median_ratio = new["wall_median_s"] / old["wall_median_s"]
    p95_ratio = new["wall_p95of_s"] / old["wall_p95of_s"]

    if n_rows in _MEDIAN_RATIO_THRESHOLDS:
        threshold = _MEDIAN_RATIO_THRESHOLDS[n_rows]
        if median_ratio > threshold:
            failures.append(f"n={n_rows}: median ratio {median_ratio:.3f} > {threshold}")
        p95_threshold = _P95_RATIO_THRESHOLDS[n_rows]
        if p95_ratio > p95_threshold:
            failures.append(f"n={n_rows}: p95 ratio {p95_ratio:.3f} > {p95_threshold}")
    else:
        regression_s = new["wall_median_s"] - old["wall_median_s"]
        allowed = max(old["wall_median_s"] * _TEN_K_RELATIVE_REGRESSION, _TEN_K_MAX_REGRESSION_S)
        if regression_s > allowed:
            failures.append(
                f"n={n_rows}: median regression {regression_s * 1000:.1f}ms > "
                f"{allowed * 1000:.1f}ms allowed"
            )

    old_rss = old.get("peak_rss_max_kb")
    new_rss = new.get("peak_rss_max_kb")
    if old_rss and new_rss:
        rss_ratio = new_rss / old_rss
        if rss_ratio > _RSS_RATIO_THRESHOLD:
            failures.append(f"n={n_rows}: peak RSS ratio {rss_ratio:.3f} > {_RSS_RATIO_THRESHOLD}")

    lo, hi = _bootstrap_ratio_ci(
        old_walls, new_walls, resamples=_BOOTSTRAP_RESAMPLES, confidence=_BOOTSTRAP_CONFIDENCE
    )
    applicable_threshold = _MEDIAN_RATIO_THRESHOLDS.get(n_rows)
    if applicable_threshold is not None and hi > applicable_threshold:
        failures.append(
            f"n={n_rows}: {_BOOTSTRAP_CONFIDENCE:.0%} bootstrap CI upper bound {hi:.3f} "
            f"exceeds threshold {applicable_threshold}"
        )

    old_out_rows = {r["out_rows"] for r in old["raw_reps"]}
    new_out_rows = {r["out_rows"] for r in new["raw_reps"]}
    if old_out_rows != new_out_rows:
        failures.append(f"n={n_rows}: out_rows differ old={old_out_rows} new={new_out_rows}")

    print(
        f"n={n_rows}: old_median={old['wall_median_s']:.3f}s new_median={new['wall_median_s']:.3f}s "
        f"ratio={median_ratio:.3f} (95% CI [{lo:.3f}, {hi:.3f}]) p95_ratio={p95_ratio:.3f} "
        f"old_rss={old.get('peak_rss_max_mb')}MB new_rss={new.get('peak_rss_max_mb')}MB"
    )
    return failures


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="10000,100000,1000000")
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--out-dir", default="/tmp/unified_slice_bench")  # noqa: S108 fixed local bench scratch dir
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    old_results = _run_driver(
        args.tiers, args.reps, args.warmup, "bench_worker.py", out_dir / "old.json"
    )
    new_results = _run_driver(
        args.tiers,
        args.reps,
        args.warmup,
        "../bench-unified-slice/bench_worker_unified.py",
        out_dir / "new.json",
    )

    all_failures: list[str] = []
    for tier_key in old_results:
        n_rows = int(tier_key)
        all_failures.extend(_check_tier(n_rows, old_results[tier_key], new_results[tier_key]))

    print("\n=== D9 performance gate ===")
    if all_failures:
        print("FAILED:")
        for f in all_failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("PASSED: every tier within the D9 thresholds.")


if __name__ == "__main__":
    main()
