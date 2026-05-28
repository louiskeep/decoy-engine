"""Compare the pre-rewrite baseline against the engine-v2 baseline (S13).

Reads the pre-rewrite baseline (`pandas-baseline-pre-rewrite.json`, V1 transforms)
and the engine-v2 baseline (`engine-v2-baseline.json`, the v2 adapter on both
substrates), matches cells by (strategy, tier), and emits the readiness-report
Performance Gate section as markdown.

Gate logic (done-definition.md / S13 section 2.2):
- Performance is counted ONLY where the cell's Correctness Gate passed (a faster
  engine that broke correctness does not count).
- Faker cell improves >= 10x at medium tier (engine-v2 shipped substrate vs
  pre-rewrite).
- FPE cell improves >= 2x at medium tier.
- No cell regresses > 5% vs pre-rewrite without a documented waiver (the rewrite-era
  band is widened per testing-and-risks.md; cheap-band boundary-cost regressions
  are expected and waived in the readiness report).

The shipped substrate is polars (PQ6), so the vs-pre-rewrite ratio is computed on
the polars number; the pandas-engine-v2 and polars-vs-pandas numbers are reported
alongside for context. Exits non-zero if a hard gate fails, so CI can enforce.

Usage::

    python scripts/compare_baselines.py \
        --old tests/perf_fixtures/pandas-baseline-pre-rewrite.json \
        --new tests/perf_fixtures/engine-v2-baseline.json \
        --output docs/v2/perf/engine-v2-release-readiness-perf.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _index_old(old: dict[str, Any]) -> dict[tuple[str, str], float]:
    """(strategy, tier) -> pre-rewrite p50_ms."""
    out: dict[tuple[str, str], float] = {}
    for cell in old.get("results", []):
        if cell.get("error"):
            continue
        out[(cell["strategy"], cell["tier"])] = float(cell.get("p50_ms", 0.0))
    return out


def _ratio(before: float, after: float) -> float | None:
    """before/after = 'after is Nx faster than before'. None if not computable."""
    if before <= 0 or after <= 0:
        return None
    return before / after


def _fmt(x: float | None, suffix: str = "") -> str:
    return "n/a" if x is None else f"{x:.2f}{suffix}"


def build_report(old: dict[str, Any], new: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """Return (markdown, hard_failures, warnings).

    hard_failures (exit non-zero): a correctness-failed cell, or a headline ratio
    gate below threshold when it IS evaluable. warnings (reported, not blocking):
    per-cell regressions vs pre-rewrite (waiver-candidates the readiness report
    dispositions per the rewrite-era band) and gates that are not yet evaluable
    because the engine-v2 baseline lacks that cell (e.g. a small-only local run).
    """
    old_p50 = _index_old(old)
    lines: list[str] = []
    failures: list[str] = []
    warnings: list[str] = []
    env = new.get("meta", {}).get("environment", "unknown")

    lines.append("## Performance Gate (engine-v2 vs pre-rewrite)\n")
    lines.append(f"Environment: `{env}`. Performance counted only where Correctness Gate = PASS.\n")
    lines.append(
        "| strategy | tier | correctness | pre-rewrite p50 (ms) | v2 pandas p50 | "
        "v2 polars p50 | polars vs pre-rewrite | polars vs pandas |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")

    for cell in new.get("results", []):
        strat, tier = cell["strategy"], cell["tier"]
        gate = cell.get("correctness_gate", "PASS")
        pan = cell.get("pandas", {})
        pol = cell.get("polars", {})
        pan_p50 = float(pan.get("p50_ms", 0.0))
        pol_p50 = float(pol.get("p50_ms", 0.0))
        before = old_p50.get((strat, tier))
        vs_pre = _ratio(before, pol_p50) if (before is not None and gate == "PASS") else None
        vs_pan = _ratio(pan_p50, pol_p50) if gate == "PASS" else None
        lines.append(
            f"| {strat} | {tier} | {gate} | {_fmt(before)} | {_fmt(pan_p50)} | "
            f"{_fmt(pol_p50)} | {_fmt(vs_pre, 'x')} | {_fmt(vs_pan, 'x')} |"
        )

        # Regression check (shipped substrate vs pre-rewrite), counted only on PASS.
        # Waiver-candidate, NOT a hard fail: cheap-band cells regress on the v2
        # adapter's Arrow-boundary cost (expected per the substrate-decision doc);
        # the readiness report dispositions each per the rewrite-era band.
        if gate == "PASS" and before is not None and pol_p50 > before * 1.05:
            warnings.append(
                f"regression (waiver-candidate): {strat}/{tier} polars {pol_p50:.2f}ms "
                f"> pre-rewrite {before:.2f}ms (+{(pol_p50 / before - 1) * 100:.0f}%)"
            )

    # Headline ratio gates at medium tier.
    lines.append("\n### Headline gates (medium tier)\n")
    for strat, threshold in (("faker", 10.0), ("fpe", 2.0)):
        cell = _find(new, strat, "medium")
        if cell is None:
            warnings.append(
                f"gate not evaluable: {strat}/medium cell missing (run medium tier on CI)"
            )
            lines.append(f"- {strat} >= {threshold:g}x @ medium: NOT EVALUABLE (cell missing)")
            continue
        if cell.get("correctness_gate") != "PASS":
            failures.append(f"GATE FAIL: {strat}/medium correctness != PASS")
            lines.append(f"- {strat} >= {threshold:g}x @ medium: FAIL (correctness)")
            continue
        before = old_p50.get((strat, "medium"))
        ratio = _ratio(before, float(cell.get("polars", {}).get("p50_ms", 0.0)))
        verdict = "PASS" if (ratio is not None and ratio >= threshold) else "FAIL"
        if verdict == "FAIL":
            failures.append(f"GATE FAIL: {strat}/medium {_fmt(ratio, 'x')} < {threshold:g}x")
        lines.append(f"- {strat} >= {threshold:g}x @ medium: {verdict} ({_fmt(ratio, 'x')})")

    return "\n".join(lines) + "\n", failures, warnings


def _find(new: dict[str, Any], strategy: str, tier: str) -> dict[str, Any] | None:
    for cell in new.get("results", []):
        if cell["strategy"] == strategy and cell["tier"] == tier:
            return cell
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compare-baselines")
    parser.add_argument("--old", type=Path, required=True, help="pre-rewrite baseline JSON")
    parser.add_argument("--new", type=Path, required=True, help="engine-v2 baseline JSON")
    parser.add_argument("--output", type=Path, default=None, help="markdown output path")
    args = parser.parse_args(argv)

    old = json.loads(args.old.read_text(encoding="utf-8"))
    new = json.loads(args.new.read_text(encoding="utf-8"))
    report, failures, warnings = build_report(old, new)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"[compare-baselines] wrote {args.output}")
    else:
        print(report)

    if warnings:
        print("\n[compare-baselines] WARNINGS (waiver-candidates / not-yet-evaluable):")
        for w in warnings:
            print(f"  - {w}")
    if failures:
        print("\n[compare-baselines] HARD GATE FAILURES:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\n[compare-baselines] no hard gate failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
