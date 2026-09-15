"""Task 4.5 D9 performance gate: the paired old/new benchmark comparison.

Runs `scripts/native-baseline/bench_driver.py` twice per tier over the SAME
nine-column `bench_worker_unified.py` source -- once with the lane OFF (the
legacy route, `UNIFIED_BENCH_FLAG=off`, the "old" arm) and once with it ON
(the unified lane, the "new" arm) -- ALTERNATING tier by tier so a host load
spike does not land entirely inside one arm. Both arms therefore mask one
identical workload (a per-tier `workload_fingerprint` is asserted equal), so
the ratio reflects the lane and nothing else. Then it checks the D9 thresholds:

  - 100k & 1M: median new/old wall <= 1.10, p95 <= 1.15
  - 10k: median regression <= max(10%, 50 ms)
  - peak RSS <= 1.10x at every tier
  - flag-off overhead vs main <= 1% median: pass `--baseline-old` a prior
    flag-off (legacy-arm) result recorded on `origin/main` with the SAME
    nine-column worker; the fingerprints must match across revisions or the
    check fails rather than comparing two different workloads.

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
import os
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


def _run_driver(
    tiers: str, reps: int, warmup: int, worker: str, out_path: Path, flag: str = "on"
) -> dict:
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
    # Select the arm this driver's worker times. Both arms run the SAME
    # nine-column unified-slice worker/source; `flag` only toggles whether that
    # worker routes through the unified lane ("on") or the legacy route ("off"),
    # so the comparison is over one identical workload rather than two.
    env = {**os.environ, "UNIFIED_BENCH_FLAG": flag}
    subprocess.run(cmd, check=True, env=env)  # noqa: S603 fixed local benchmark command
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


_NULL_FINGERPRINT = json.dumps(None)


def _fingerprint_set(tier_result: dict) -> set[str]:
    """The distinct workload fingerprints recorded across a tier's reps."""
    return {
        json.dumps(r.get("workload_fingerprint"), sort_keys=True)
        for r in tier_result.get("raw_reps", [])
    }


def _fingerprint_failures(n_rows: int, a_label: str, a: dict, b_label: str, b: dict) -> list[str]:
    """Reject a missing/null fingerprint, a fingerprint that varies within an
    arm, or a mismatch between arms -- so a comparison over two DIFFERENT
    workloads (or over reps that never recorded what they masked) fails loudly
    instead of passing vacuously."""
    a_fp, b_fp = _fingerprint_set(a), _fingerprint_set(b)
    if _NULL_FINGERPRINT in a_fp or _NULL_FINGERPRINT in b_fp:
        return [
            f"n={n_rows}: a rep recorded no workload_fingerprint ({a_label}={a_fp} {b_label}={b_fp})"
        ]
    if len(a_fp) != 1 or len(b_fp) != 1:
        return [
            f"n={n_rows}: workload_fingerprint varies within an arm ({a_label}={a_fp} {b_label}={b_fp})"
        ]
    if a_fp != b_fp:
        return [
            f"n={n_rows}: {a_label} and {b_label} masked different workloads ({a_fp} vs {b_fp})"
        ]
    return []


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

    # Both arms must have masked the IDENTICAL workload; otherwise the ratio is
    # meaningless (a heavier arm looks slower for a reason that is not the lane).
    failures.extend(_fingerprint_failures(n_rows, "off arm", old, "on arm", new))

    print(
        f"n={n_rows}: old_median={old['wall_median_s']:.3f}s new_median={new['wall_median_s']:.3f}s "
        f"ratio={median_ratio:.3f} (95% CI [{lo:.3f}, {hi:.3f}]) p95_ratio={p95_ratio:.3f} "
        f"old_rss={old.get('peak_rss_max_mb')}MB new_rss={new.get('peak_rss_max_mb')}MB"
    )
    return failures


def _check_old_vs_baseline(old_results: dict, baseline: dict) -> list[str]:
    """The docstring's 4th claim: the "old" (flag-off) arm of THIS run must
    not have regressed more than 1% median versus `--baseline-old`, a prior
    `bench_driver.py` results JSON. The baseline MUST have been recorded with
    the SAME nine-column flag-off worker (`UNIFIED_BENCH_FLAG=off
    bench_worker_unified.py`): the per-tier workload fingerprints must match, or
    the comparison is apples-to-oranges (e.g. a legacy ten-column worker that
    also masked `pt_ts`) and the check fails rather than reporting a false
    regression number. A tier missing from the baseline is skipped, not failed."""
    failures: list[str] = []
    for tier_key, cur in old_results.items():
        base = baseline.get(tier_key)
        if base is None:
            continue
        fp_failures = _fingerprint_failures(int(tier_key), "this-run off", cur, "baseline", base)
        if fp_failures:
            failures.extend(fp_failures)
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
    unified_worker = "../bench-unified-slice/bench_worker_unified.py"
    for n_rows in tiers:
        tier_arg = str(n_rows)
        # Both arms are the SAME nine-column unified-slice worker; the env flag
        # only routes it through the legacy ("off") vs unified ("on") path, so
        # neither arm does more work than the other (the pt_ts column the old
        # 10-column worker used to carry systematically favored the new arm).
        old_tier = _run_driver(
            tier_arg, args.reps, args.warmup, unified_worker, out_dir / f"old_{n_rows}.json", "off"
        )
        new_tier = _run_driver(
            tier_arg, args.reps, args.warmup, unified_worker, out_dir / f"new_{n_rows}.json", "on"
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
