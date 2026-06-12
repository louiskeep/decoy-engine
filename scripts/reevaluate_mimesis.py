"""Re-run the Mimesis adoption evaluation and diff against the matrix.

The standing procedure (docs/mimesis-adoption-2026-06-12.md): whenever
the `mimesis` pin moves past `>=19.0,<20`, run this script with the
candidate version installed. It executes `run_parity_suite` for every
provider in MIMESIS_CANDIDATES and prints a verdict table plus the
adoption diff against ADOPTED_MIMESIS_PROVIDERS.

Exit codes: 0 when the verdicts match the current matrix, 1 on drift
(a provider should be added or removed). NOT run in CI: the speed gate
(check 7's sibling, benchmark_ratio < 0.20) is timing-noisy, which is
exactly why the seeded CI tripwire asserts only checks 1-6.

Usage:
    python scripts/reevaluate_mimesis.py [--locale en_US] [--n 10000] [--json]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys


def evaluate(locale: str, n: int) -> dict[str, list]:
    from decoy_engine.providers_v2.mimesis._adoption_matrix import MIMESIS_CANDIDATES
    from decoy_engine.providers_v2.mimesis._parity import run_parity_suite

    return {
        provider: run_parity_suite(provider, locale=locale, n=n)
        for provider in sorted(MIMESIS_CANDIDATES)
    }


def adoption_diff(results: dict[str, list]) -> tuple[frozenset[str], frozenset[str]]:
    """Return (to_add, to_remove) vs the current adoption matrix."""
    from decoy_engine.providers_v2.mimesis._adoption_matrix import ADOPTED_MIMESIS_PROVIDERS
    from decoy_engine.providers_v2.mimesis._parity import is_adoptable

    adoptable = frozenset(p for p, checks in results.items() if is_adoptable(checks))
    return (
        adoptable - ADOPTED_MIMESIS_PROVIDERS,
        ADOPTED_MIMESIS_PROVIDERS - adoptable,
    )


def _render_table(results: dict[str, list]) -> str:
    from decoy_engine.providers_v2.mimesis._parity import is_adoptable

    lines = [
        f"{'provider':<22} {'ratio':>7}  {'checks 1-6':<10} {'check 7':<9} verdict",
        "-" * 64,
    ]
    for provider, checks in results.items():
        ratio = next((c.benchmark_ratio for c in checks if c.benchmark_ratio is not None), None)
        gating = [c for c in checks if c.check != "distribution"]
        advisory = [c for c in checks if c.check == "distribution"]
        gating_ok = all(c.passed for c in gating)
        advisory_ok = all(c.passed for c in advisory)
        verdict = "ADOPT" if is_adoptable(checks) else "reject"
        lines.append(
            f"{provider:<22} {ratio if ratio is not None else float('nan'):>7.3f}  "
            f"{'pass' if gating_ok else 'FAIL':<10} "
            f"{'pass' if advisory_ok else 'review':<9} {verdict}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--locale", default="en_US")
    parser.add_argument("--n", type=int, default=10_000)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    if importlib.util.find_spec("mimesis") is None:
        print(
            "mimesis is not installed; install the extra under evaluation first:\n"
            "    uv pip install 'decoy-engine[mimesis]'",
            file=sys.stderr,
        )
        return 2

    import mimesis as _mimesis

    results = evaluate(args.locale, args.n)
    to_add, to_remove = adoption_diff(results)

    if args.as_json:
        print(
            json.dumps(
                {
                    "mimesis_version": getattr(_mimesis, "__version__", "unknown"),
                    "locale": args.locale,
                    "n": args.n,
                    "results": {
                        p: [
                            {
                                "check": c.check,
                                "passed": c.passed,
                                "benchmark_ratio": c.benchmark_ratio,
                                "detail": c.detail,
                            }
                            for c in checks
                        ]
                        for p, checks in results.items()
                    },
                    "to_add": sorted(to_add),
                    "to_remove": sorted(to_remove),
                },
                indent=1,
            )
        )
    else:
        print(
            f"mimesis {getattr(_mimesis, '__version__', 'unknown')}, "
            f"locale={args.locale}, n={args.n}\n"
        )
        print(_render_table(results))
        print()
        if to_add or to_remove:
            if to_add:
                print(f"DRIFT: now adoption-eligible (not in matrix): {', '.join(sorted(to_add))}")
            if to_remove:
                print(f"DRIFT: adopted but no longer passing: {', '.join(sorted(to_remove))}")
            print(
                "Update ADOPTED_MIMESIS_PROVIDERS and append a dated results "
                "table to docs/mimesis-adoption-2026-06-12.md."
            )
        else:
            print("No drift: the adoption matrix matches this run.")

    return 1 if (to_add or to_remove) else 0


if __name__ == "__main__":
    raise SystemExit(main())
