"""Run the PERF.BASE.3 pandas baseline matrix.

For each (strategy, tier) cell:

- Loads the fixture once (cost charged to setup, not the cell).
- Applies the strategy's rule N times via
  ``StrategyManager.apply_masking_rules`` with a bound TimingCollector.
- Collects wall-clock per iteration + RSS delta + CPU sample.
- Aggregates: count, p50_ms, p95_ms, mean_ms, max_ms, peak_delta_kb,
  cpu_percent_sample, error.
- Writes the matrix to ``tests/perf_fixtures/pandas-baseline.json``.

Usage::

    python scripts/run_perf_baseline.py                       # all tiers
    python scripts/run_perf_baseline.py --tier small
    python scripts/run_perf_baseline.py --tier medium --iterations 3
    python scripts/run_perf_baseline.py --strategy fpe --tier medium

The script is CLI-shaped and lives outside the engine package; the
engine never imports it. Tests under ``tests/perf_fixtures/`` import
``strategy_rules`` + ``loaders`` only.

Source pattern: per-call elapsed via ``time.perf_counter`` (already
the engine-side timing primitive in PERF.BASE.1), RSS via
``psutil.Process.memory_info().rss`` (matches the engine's
``decoy_engine.instrumentation.rss_kb``). py-spy flame graph capture
for slow cells is a separate sub-step run manually (see
``docs/v2/perf/pandas-baseline-report.md`` "Hot-spot analysis").
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

# Make tests/ importable so we can pull schema + strategy_rules.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from decoy_engine.instrumentation import TimingCollector, rss_kb, use_collector  # noqa: E402
from decoy_engine.transforms.registry import StrategyManager  # noqa: E402
from perf_fixtures.loaders import available_tiers, load_tier  # noqa: E402
from perf_fixtures.schema import TIERS  # noqa: E402
from perf_fixtures.strategy_rules import (  # noqa: E402
    BaselineCell,
    included_cells,
    skipped_cells,
)

_BASELINE_JSON = _REPO_ROOT / "tests" / "perf_fixtures" / "pandas-baseline.json"

# Quiet the engine logger so the harness output is readable. The
# strategy modules log INFO per masking-rule application; at 100k rows
# that's a 12-line spam block per iteration.
logging.getLogger("decoy_engine").setLevel(logging.WARNING)


@dataclass
class CellResult:
    """Aggregated timing for one (strategy, tier) cell."""

    strategy: str
    tier: str
    column: str
    rows: int
    iterations: int
    p50_ms: float
    p95_ms: float
    mean_ms: float
    max_ms: float
    peak_delta_kb: int
    rss_baseline_kb: int
    rss_after_kb: int
    cpu_percent_sample: float
    error: str | None = None


def _p(values: list[float], pct: float) -> float:
    """Return the ``pct`` percentile of ``values`` (linear interpolation)."""
    if not values:
        return 0.0
    return float(statistics.quantiles(values, n=100, method="inclusive")[int(pct) - 1])


def _run_cell(
    cell: BaselineCell,
    tier_name: str,
    iterations: int,
    process: psutil.Process,
) -> CellResult:
    """Run one (strategy, tier) cell ``iterations`` times.

    Returns a ``CellResult``; on error the timing fields are 0 and
    ``error`` carries the exception string.
    """
    df = load_tier(tier_name)
    rows = len(df)
    rss_before = rss_kb()
    # cpu_percent baseline call -- the next call (after the work)
    # returns the percent since this call.
    process.cpu_percent(interval=None)

    collector = TimingCollector()
    rules = [cell.rule]

    try:
        # Warmup iteration -- pays JIT / import / first-call costs that
        # would skew percentiles. Discarded. ALSO used to budget: if
        # warmup is slow, drop iteration count so a full matrix run on
        # medium completes in reasonable wall time. Faker at 16ms/row
        # would otherwise take ~27min per iteration on the 100k-row
        # medium tier; clamping keeps the run human-scale while still
        # giving real timing data.
        warmup_t0 = time.perf_counter()
        manager = StrategyManager(seed=20260526, logger=logging.getLogger("decoy_engine"))
        manager.apply_masking_rules(df, rules)
        warmup_ms = (time.perf_counter() - warmup_t0) * 1000.0

        if warmup_ms > 30_000:
            actual_iterations = 1  # 30s+: just one timed pass
        elif warmup_ms > 5_000:
            actual_iterations = min(iterations, 2)
        else:
            actual_iterations = iterations

        with use_collector(collector):
            for _ in range(actual_iterations):
                # Re-create the manager each iteration so per-call cache
                # state (e.g. FPE charset prep) is paid each time -- we
                # measure the strategy primitive, not a warm cache.
                manager = StrategyManager(
                    seed=20260526, logger=logging.getLogger("decoy_engine")
                )
                manager.apply_masking_rules(df, rules)
    except Exception as exc:  # pragma: no cover -- defensive
        return CellResult(
            strategy=cell.strategy,
            tier=tier_name,
            column=cell.column,
            rows=rows,
            iterations=0,
            p50_ms=0.0,
            p95_ms=0.0,
            mean_ms=0.0,
            max_ms=0.0,
            peak_delta_kb=0,
            rss_baseline_kb=rss_before,
            rss_after_kb=rss_kb(),
            cpu_percent_sample=0.0,
            error=f"{type(exc).__name__}: {exc}",
        )

    elapsed = [r.elapsed_ms for r in collector.records]
    rss_after = rss_kb()
    cpu_pct = process.cpu_percent(interval=None)

    return CellResult(
        strategy=cell.strategy,
        tier=tier_name,
        column=cell.column,
        rows=rows,
        iterations=len(elapsed),
        p50_ms=round(statistics.median(elapsed), 3) if elapsed else 0.0,
        p95_ms=round(_p(elapsed, 95), 3) if len(elapsed) >= 2 else round(max(elapsed), 3),
        mean_ms=round(statistics.fmean(elapsed), 3) if elapsed else 0.0,
        max_ms=round(max(elapsed), 3) if elapsed else 0.0,
        peak_delta_kb=max((r.peak_memory_delta_kb for r in collector.records), default=0),
        rss_baseline_kb=rss_before,
        rss_after_kb=rss_after,
        cpu_percent_sample=round(cpu_pct, 2),
    )


def run_matrix(
    tier_names: list[str],
    strategy_filter: str | None,
    iterations: int,
) -> list[CellResult]:
    """Run the (strategy, tier) cross product and return the results."""
    process = psutil.Process()
    cells = included_cells()
    if strategy_filter:
        cells = tuple(c for c in cells if c.strategy == strategy_filter)
        if not cells:
            raise SystemExit(f"unknown strategy {strategy_filter!r}")

    results: list[CellResult] = []
    for tier in tier_names:
        if tier not in available_tiers():
            print(
                f"[perf-baseline] SKIP tier={tier} (fixture missing on disk; "
                f"regenerate via `python scripts/gen_perf_fixtures.py {tier}`)"
            )
            continue
        print(f"[perf-baseline] tier={tier} starting ({len(cells)} cells)")
        for cell in cells:
            print(
                f"[perf-baseline] tier={tier} strategy={cell.strategy} column={cell.column}",
                flush=True,
            )
            result = _run_cell(cell, tier, iterations, process)
            if result.error:
                print(f"  ERROR: {result.error}")
            else:
                print(
                    f"  p50={result.p50_ms:.1f}ms p95={result.p95_ms:.1f}ms "
                    f"mean={result.mean_ms:.1f}ms peak_delta_kb={result.peak_delta_kb} "
                    f"cpu={result.cpu_percent_sample:.0f}%"
                )
            results.append(result)
    return results


def write_results(results: list[CellResult], out_path: Path) -> None:
    """Persist results JSON in a stable, diff-friendly shape.

    Top-level keys: meta (engine version stamp), results (list of
    CellResult dicts), skipped (cells dropped from the matrix with
    their skip_reason). The nested-by-tier-then-strategy shape the
    sprint spec sketched (``{strategy: {tier: {...}}}``) is reconstructed
    by readers from the flat ``results`` list; persisting the flat
    list keeps add / remove / reorder operations clean in diffs.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "meta": {
            "schema_version": 1,
            "tool": "scripts/run_perf_baseline.py",
        },
        "results": [asdict(r) for r in results],
        "skipped": [
            {"strategy": c.strategy, "reason": c.skip_reason}
            for c in skipped_cells()
        ],
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    print(f"[perf-baseline] wrote {len(results)} cells to {out_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run-perf-baseline")
    parser.add_argument(
        "--tier",
        choices=[*sorted(TIERS), "all"],
        default="all",
        help="which fixture tier to run; default 'all'",
    )
    parser.add_argument(
        "--strategy",
        default=None,
        help="restrict the matrix to one strategy name",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="iterations per cell (excluding 1 discarded warmup); default 5",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_BASELINE_JSON,
        help=f"output JSON path; default {_BASELINE_JSON}",
    )
    args = parser.parse_args(argv)

    tier_names = sorted(TIERS) if args.tier == "all" else [args.tier]
    t0 = time.perf_counter()
    results = run_matrix(tier_names, args.strategy, args.iterations)
    elapsed = time.perf_counter() - t0
    write_results(results, args.out)
    print(f"[perf-baseline] total wall time {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
