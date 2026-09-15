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
_OLD_VS_BASELINE_MAX_REGRESSION = 0.01  # the "flag-off overhead vs main <= 1%" claim above


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


def _check_old_vs_baseline(old_results: dict, baseline: dict) -> list[str]:
    """The docstring's 4th claim: the "old" (flag-off) arm of THIS run must
    not have regressed more than 1% median versus `--baseline-old`, a prior
    `bench_driver.py` results JSON recorded for the unmodified oracle (e.g.
    on `origin/main`). A tier missing from the baseline is skipped, not
    failed -- comparing against a baseline that never measured it would be a
    false claim, not a real check."""
    failures: list[str] = []
    for tier_key, cur in old_results.items():
        base = baseline.get(tier_key)
        if base is None:
            continue
        ratio = cur["wall_median_s"] / base["wall_median_s"]
        if ratio > 1 + _OLD_VS_BASELINE_MAX_REGRESSION:
            failures.append(
                f"n={tier_key}: flag-off arm regressed {(ratio - 1) * 100:.2f}% vs "
                f"--baseline-old (median {cur['wall_median_s']:.3f}s vs "
                f"{base['wall_median_s']:.3f}s, {_OLD_VS_BASELINE_MAX_REGRESSION:.0%} allowed)"
            )
    return failures


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="10000,100000,1000000")
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--out-dir", default="/tmp/unified_slice_bench")  # noqa: S108 fixed local bench scratch dir
    ap.add_argument(
        "--baseline-old",
        default=None,
        help=(
            "path to a bench_driver.py results JSON recorded for the unmodified "
            "bench_worker.py oracle on a prior commit (e.g. origin/main); when given, "
            "checks this run's flag-off ('old') arm against it for the <=1%% median "
            "regression the D9 gate requires"
        ),
    )
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tiers = [int(x) for x in args.tiers.split(",")]
    old_results: dict[str, dict] = {}
    new_results: dict[str, dict] = {}
    # ALTERNATE old/new per tier (not a full old sweep followed by a full new
    # sweep): a host load spike lasting less than one tier's own sweep would
    # otherwise land entirely inside one arm and bias that arm's numbers.
    for n_rows in tiers:
        tier_arg = str(n_rows)
        old_tier = _run_driver(
            tier_arg, args.reps, args.warmup, "bench_worker.py", out_dir / f"old_{n_rows}.json"
        )
        new_tier = _run_driver(
            tier_arg,
            args.reps,
            args.warmup,
            "../bench-unified-slice/bench_worker_unified.py",
            out_dir / f"new_{n_rows}.json",
        )
        old_results.update(old_tier)
        new_results.update(new_tier)

    (out_dir / "old.json").write_text(json.dumps(old_results, indent=2))
    (out_dir / "new.json").write_text(json.dumps(new_results, indent=2))

    all_failures: list[str] = []
    for tier_key in old_results:
        n_rows = int(tier_key)
        all_failures.extend(_check_tier(n_rows, old_results[tier_key], new_results[tier_key]))

    if args.baseline_old is not None:
        baseline = json.loads(Path(args.baseline_old).read_text())
        all_failures.extend(_check_old_vs_baseline(old_results, baseline))

    print("\n=== D9 performance gate ===")
    if all_failures:
        print("FAILED:")
        for f in all_failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("PASSED: every tier within the D9 thresholds.")


if __name__ == "__main__":
    main()
