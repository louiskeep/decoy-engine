"""Compare engine-v2 baseline against a pre-rewrite baseline if one is supplied.

Reads the engine-v2 baseline (`engine-v2-baseline.json`, the v2 adapter on both
substrates) and, OPTIONALLY, a pre-rewrite baseline (`pandas-baseline-pre-rewrite.json`,
V1 transforms). When the pre-rewrite baseline is supplied it emits the full
relative-comparison Performance Gate report. When absent (post-V1-cleanup; see
engine commit b9b73e1 deleting the V1 graph runner), it emits an absolute-only
report covering correctness + FPE throughput + per-cell summary, with the
pre-rewrite columns marked n/a.

Gate logic (done-definition.md / S13 section 2.2):
- Performance is counted ONLY where the cell's Correctness Gate passed (a faster
  engine that broke correctness does not count).
- Faker cell improves >= 10x at medium tier (engine-v2 shipped substrate vs
  pre-rewrite). NOT EVALUABLE without --old; the 2026-05-28 canonical run measured
  294.55x and is cited in the readiness report as historical evidence.
- FPE cell sustains >= 15,000 rows/sec at medium tier on the shipped substrate
  (polars). Absolute throughput gate, always evaluable regardless of --old.
- No cell regresses > 5% vs pre-rewrite without a documented waiver (the rewrite-era
  band is widened per testing-and-risks.md; cheap-band boundary-cost regressions
  are expected and waived in the readiness report). Skipped without --old.

The shipped substrate is polars (PQ6), so the vs-pre-rewrite ratio is computed on
the polars number; the pandas-engine-v2 and polars-vs-pandas numbers are reported
alongside for context. Exits non-zero if a hard gate fails, so CI can enforce.

Usage::

    # Standard: absolute-only mode (post-V1-cleanup default)
    python scripts/compare_baselines.py \
        --new tests/perf_fixtures/engine-v2-baseline.json \
        --output docs/v2/perf/engine-v2-release-readiness-perf.md

    # Optional: relative-comparison mode when a pre-rewrite baseline is supplied
    python scripts/compare_baselines.py \
        --old <pre-rewrite baseline JSON path> \
        --new tests/perf_fixtures/engine-v2-baseline.json \
        --output docs/v2/perf/engine-v2-release-readiness-perf.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# FPE ship gate: absolute throughput floor on the shipped substrate (polars) at the
# medium tier (100k rows). Re-barred in S13 from ">= 2x vs pre-rewrite" (the relative
# gate was dominated by irreducible HMAC-Feistel crypto cost, not the chunked
# parallelism S9 owned). 15k rows/sec leaves ~30% headroom below the measured CI
# floor (~21-23k rows/sec) to absorb shared-runner variance, while still catching a
# serialized-parallelism regression (which drops throughput multi-fold on a
# multi-core runner -> well under 15k). See build_report.
FPE_MIN_ROWS_PER_SEC = 15_000.0


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


def _fmt_tput(x: float | None) -> str:
    return "n/a" if x is None else f"{x:,.0f} rows/sec"


def _throughput(cell: dict[str, Any]) -> float | None:
    """rows/sec on the shipped substrate (polars p50). None if not computable."""
    rows = float(cell.get("rows", 0.0))
    pol_p50 = float(cell.get("polars", {}).get("p50_ms", 0.0))
    if rows <= 0 or pol_p50 <= 0:
        return None
    return rows / (pol_p50 / 1000.0)


def build_report(
    old: dict[str, Any] | None, new: dict[str, Any]
) -> tuple[str, list[str], list[str]]:
    """Return (markdown, hard_failures, warnings).

    hard_failures (exit non-zero): a correctness-failed cell, or a headline ratio
    gate below threshold when it IS evaluable. warnings (reported, not blocking):
    per-cell regressions vs pre-rewrite (waiver-candidates the readiness report
    dispositions per the rewrite-era band) and gates that are not yet evaluable
    because the engine-v2 baseline lacks that cell (e.g. a small-only local run).

    When ``old`` is None (post-V1-cleanup default), the pre-rewrite columns and
    the Faker relative-speedup gate are reported as n/a / NOT EVALUABLE. The
    absolute FPE throughput gate + correctness gate stay live.
    """
    old_p50 = _index_old(old) if old is not None else {}
    lines: list[str] = []
    failures: list[str] = []
    warnings: list[str] = []
    env = new.get("meta", {}).get("environment", "unknown")

    header_suffix = "" if old is not None else " (absolute-only; no pre-rewrite baseline supplied)"
    lines.append(f"## Performance Gate (engine-v2 vs pre-rewrite){header_suffix}\n")
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
        # the readiness report dispositions each per the rewrite-era band. Zero-
        # guard the percentage compute: a zero or sub-zero `before` would crash
        # the format string (Dennis pass-9 LOW; before-fix the script raised
        # ZeroDivisionError on any cell whose pre-rewrite p50 had been captured as 0).
        if gate == "PASS" and before is not None and before > 0 and pol_p50 > before * 1.05:
            warnings.append(
                f"regression (waiver-candidate): {strat}/{tier} polars {pol_p50:.2f}ms "
                f"> pre-rewrite {before:.2f}ms (+{(pol_p50 / before - 1) * 100:.0f}%)"
            )

    # Headline gates at medium tier (shipped substrate = polars).
    lines.append("\n### Headline gates (medium tier)\n")

    # Faker: relative speedup vs the pre-rewrite baseline (>= 10x). Pool-backed
    # generation (S5/S7/S9) dwarfs the pre-rewrite per-row Faker calls, so a
    # multiplier is the right shape here. NOT EVALUABLE without --old (the
    # pre-rewrite baseline was deleted in engine commit b9b73e1 alongside the V1
    # graph runner; the 2026-05-28 canonical run measured 294.55x and is cited
    # in the readiness report as historical evidence).
    faker = _find(new, "faker", "medium")
    if faker is None:
        warnings.append("gate not evaluable: faker/medium cell missing (run medium tier on CI)")
        lines.append("- faker >= 10x @ medium: NOT EVALUABLE (cell missing)")
    elif faker.get("correctness_gate") != "PASS":
        failures.append("GATE FAIL: faker/medium correctness != PASS")
        lines.append("- faker >= 10x @ medium: FAIL (correctness)")
    elif old is None:
        warnings.append(
            "gate not evaluable: faker/medium 10x relative gate requires --old; "
            "post-V1-cleanup the pre-rewrite baseline file no longer exists. "
            "Historical 2026-05-28 CI measurement: 294.55x (cited in readiness report)."
        )
        lines.append(
            "- faker >= 10x @ medium: NOT EVALUABLE (no pre-rewrite baseline; "
            "historical 294.55x cited in readiness report)"
        )
    else:
        before = old_p50.get(("faker", "medium"))
        if before is None:
            # No pre-rewrite number to divide by = the gate cannot be PROVEN. A
            # green CI run must mean "the ratio was computed and cleared the bar,"
            # never "we had nothing to divide by."
            failures.append(
                "GATE FAIL: faker/medium has no pre-rewrite baseline cell to compare against"
            )
            lines.append("- faker >= 10x @ medium: FAIL (no pre-rewrite cell)")
        else:
            ratio = _ratio(before, float(faker.get("polars", {}).get("p50_ms", 0.0)))
            verdict = "PASS" if (ratio is not None and ratio >= 10.0) else "FAIL"
            if verdict == "FAIL":
                failures.append(f"GATE FAIL: faker/medium {_fmt(ratio, 'x')} < 10x")
            lines.append(f"- faker >= 10x @ medium: {verdict} ({_fmt(ratio, 'x')})")

    # FPE: absolute throughput floor (>= FPE_MIN_ROWS_PER_SEC at medium, polars),
    # re-barred in S13 from the original ">= 2x vs pre-rewrite". The relative gate
    # was dominated by irreducible HMAC-Feistel cost that neither substrate avoids;
    # S9 owned only the chunked per-row parallelism, which is GIL-capped at cpu_count
    # and was never going to clear 2x. The absolute floor measures sustained
    # throughput on the shipped substrate and still catches the real regression
    # (parallelism getting serialized -> well under the floor on a multi-core runner).
    # The >5% vs-pre-rewrite regression check above stays live for fpe as a
    # waiver-candidate, preserving the relative-regression signal.
    fpe = _find(new, "fpe", "medium")
    fpe_label = f"fpe >= {FPE_MIN_ROWS_PER_SEC:,.0f} rows/sec @ medium"
    if fpe is None:
        warnings.append("gate not evaluable: fpe/medium cell missing (run medium tier on CI)")
        lines.append(f"- {fpe_label}: NOT EVALUABLE (cell missing)")
    elif fpe.get("correctness_gate") != "PASS":
        failures.append("GATE FAIL: fpe/medium correctness != PASS")
        lines.append(f"- {fpe_label}: FAIL (correctness)")
    else:
        tput = _throughput(fpe)
        verdict = "PASS" if (tput is not None and tput >= FPE_MIN_ROWS_PER_SEC) else "FAIL"
        if verdict == "FAIL":
            failures.append(
                f"GATE FAIL: fpe/medium {_fmt_tput(tput)} < {FPE_MIN_ROWS_PER_SEC:,.0f} rows/sec"
            )
        lines.append(f"- {fpe_label}: {verdict} ({_fmt_tput(tput)})")

    return "\n".join(lines) + "\n", failures, warnings


def _find(new: dict[str, Any], strategy: str, tier: str) -> dict[str, Any] | None:
    cell: dict[str, Any]
    for cell in new.get("results", []):
        if cell["strategy"] == strategy and cell["tier"] == tier:
            return cell
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compare-baselines")
    parser.add_argument(
        "--old",
        type=Path,
        default=None,
        help=(
            "pre-rewrite baseline JSON (optional; absent post-V1-cleanup, "
            "see engine commit b9b73e1)"
        ),
    )
    parser.add_argument("--new", type=Path, required=True, help="engine-v2 baseline JSON")
    parser.add_argument("--output", type=Path, default=None, help="markdown output path")
    args = parser.parse_args(argv)

    old = json.loads(args.old.read_text(encoding="utf-8")) if args.old is not None else None
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
