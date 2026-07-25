"""PERF.BASE.3 (V2): structural tests for the engine-v2 baseline JSON.

The matrix is consumed by scripts/compare_baselines.py to verify the
engine-v2 performance gates. These tests guard the on-disk shape so
the comparator never has to guess at the schema, and so any accidental
clobber or field-rename surfaces immediately in CI.

Pattern: percentile-gate pattern (pytest.mark.parametrize + field-
presence + ordering assertion), per standard benchmark-report contract.

Tests:
- top-level keys present and meta.schema_version matches
- meta.substrates = ["pandas", "polars"]
- every V2 strategy appears in results for each committed tier on disk
- each result record carries all required fields
- each substrate sub-dict carries all required timing fields
- p95_ms >= p50_ms sanity gate (where iterations >= 2)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.perf

_BASELINE_PATH = (
    Path(__file__).resolve().parent.parent / "perf_fixtures" / "engine-v2-baseline.json"
)

# 11 V2 strategies included in the matrix. The 'reference' strategy is
# intentionally absent: it is I/O-bound (file-path + key_column lookup)
# rather than CPU-bound and belongs in a separate I/O matrix once that
# exists. Tracked in scripts/run_engine_v2_baseline.py V2_CELLS comments.
_V2_STRATEGIES: frozenset[str] = frozenset(
    {
        "passthrough",
        "redact",
        "truncate",
        "faker",
        "date_shift",
        "bucketize",
        "hash",
        "categorical",
        "shuffle",
        "formula",
        "fpe",
    }
)

# Committed tiers only (large is gitignored and regenerated on demand).
_COMMITTED_TIERS: frozenset[str] = frozenset({"small", "medium"})

_REQUIRED_RESULT_FIELDS: frozenset[str] = frozenset(
    {
        "strategy",
        "tier",
        "column",
        "rows",
        "correctness_gate",
        "correctness_detail",
        "pandas",
        "polars",
    }
)

_REQUIRED_SUBSTRATE_FIELDS: frozenset[str] = frozenset(
    {
        "p50_ms",
        "p95_ms",
        "mean_ms",
        "max_ms",
        "iterations",
        "boundary_conversion_ms",
        "peak_rss_delta_kb",
        "error",
    }
)


def _load_baseline() -> dict:  # type: ignore[type-arg]
    if not _BASELINE_PATH.exists():
        pytest.skip(
            f"{_BASELINE_PATH.name} not on disk; regenerate via "
            "`python scripts/run_engine_v2_baseline.py`"
        )
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def test_top_level_shape() -> None:
    """Top-level keys and schema_version must match the V2 contract."""
    data = _load_baseline()
    assert set(data.keys()) >= {"meta", "results"}, (
        f"baseline JSON missing top-level keys: have {sorted(data)}"
    )
    assert data["meta"]["schema_version"] == 1


def test_substrates() -> None:
    """meta.substrates must declare both pandas and polars in order."""
    data = _load_baseline()
    assert data["meta"]["substrates"] == ["pandas", "polars"], (
        'baseline meta.substrates must be ["pandas", "polars"]; '
        f"got {data['meta'].get('substrates')!r}"
    )


def test_every_v2_strategy_has_results_in_committed_tiers() -> None:
    """Every strategy in the V2 cell set appears for each committed tier.

    Committed tiers are the tiers whose fixtures are in the repo (small
    + medium); the large tier is gitignored and excluded from this gate.
    The check intersects the tiers actually present in the file with the
    committed-tier set, so a local partial-run (small only) does not
    fail this test.
    """
    data = _load_baseline()
    present = {(r["strategy"], r["tier"]) for r in data["results"]}
    tiers_in_file = {r["tier"] for r in data["results"]} & _COMMITTED_TIERS
    if not tiers_in_file:
        pytest.skip("no committed tiers found in baseline JSON")
    missing = [
        f"{strategy}/{tier}"
        for strategy in _V2_STRATEGIES
        for tier in tiers_in_file
        if (strategy, tier) not in present
    ]
    assert not missing, f"baseline missing cells: {sorted(missing)}"


def test_result_records_carry_required_fields() -> None:
    """Every result record must carry the full required field set."""
    data = _load_baseline()
    for r in data["results"]:
        missing = _REQUIRED_RESULT_FIELDS - set(r)
        assert not missing, (
            f"cell {r.get('strategy')!r}/{r.get('tier')!r} "
            f"missing top-level fields: {sorted(missing)}"
        )


def test_substrate_dicts_carry_required_fields() -> None:
    """Both the 'pandas' and 'polars' sub-dicts must carry all timing fields."""
    data = _load_baseline()
    for r in data["results"]:
        for substrate in ("pandas", "polars"):
            sub = r.get(substrate, {})
            missing = _REQUIRED_SUBSTRATE_FIELDS - set(sub)
            assert not missing, (
                f"cell {r.get('strategy')!r}/{r.get('tier')!r} "
                f"{substrate} sub-dict missing: {sorted(missing)}"
            )


def test_p95_at_or_above_p50() -> None:
    """p95_ms must not be below p50_ms (catches a sort / percentile bug
    in the harness). Only asserted when iterations >= 2 -- a single-
    iteration cell collapses p50 and p95 to the same value, so the
    relative ordering has no meaning there.
    """
    data = _load_baseline()
    for r in data["results"]:
        for substrate in ("pandas", "polars"):
            sub = r.get(substrate, {})
            iters = sub.get("iterations", 0)
            if iters < 2:
                continue
            p50 = sub.get("p50_ms", 0.0)
            p95 = sub.get("p95_ms", 0.0)
            assert p95 >= p50, (
                f"p95 < p50 on {r['strategy']!r}/{r['tier']!r} [{substrate}]: p50={p50} p95={p95}"
            )
