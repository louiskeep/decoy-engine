"""PERF.BASE.3: structural tests for the pandas baseline JSON.

The matrix is consumed by the substrate-change regression test (post-
PERF.BASE.3): re-run the same matrix after the Polars/DuckDB landing
and assert no cell drops more than 5% from baseline. These tests
guard the on-disk shape so that downstream comparator never has to
guess at the schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .strategy_rules import included_cells, skipped_cells

pytestmark = pytest.mark.perf

_BASELINE_PATH = Path(__file__).resolve().parent / "pandas-baseline.json"


def _load_baseline() -> dict:
    if not _BASELINE_PATH.exists():
        pytest.skip(
            f"{_BASELINE_PATH.name} not on disk; regenerate via "
            f"`python scripts/run_perf_baseline.py`"
        )
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def test_top_level_shape() -> None:
    data = _load_baseline()
    assert set(data.keys()) >= {"meta", "results", "skipped"}, (
        f"baseline JSON missing top-level keys: have {sorted(data)}"
    )
    assert data["meta"]["schema_version"] == 1


def test_every_included_strategy_has_at_least_one_cell() -> None:
    """Every strategy in the included set appears at least once in the
    results list. Tier coverage is verified separately."""
    data = _load_baseline()
    seen = {r["strategy"] for r in data["results"]}
    expected = {c.strategy for c in included_cells()}
    missing = expected - seen
    assert not missing, f"baseline missing cells for: {sorted(missing)}"


def test_skipped_cells_recorded_in_baseline() -> None:
    data = _load_baseline()
    skipped_in_json = {entry["strategy"] for entry in data["skipped"]}
    skipped_in_rules = {c.strategy for c in skipped_cells()}
    assert skipped_in_json == skipped_in_rules, (
        f"skipped-strategies drift: json={skipped_in_json} "
        f"rules={skipped_in_rules}"
    )


def test_cell_records_carry_required_fields() -> None:
    data = _load_baseline()
    required = {
        "strategy",
        "tier",
        "column",
        "rows",
        "iterations",
        "p50_ms",
        "p95_ms",
        "mean_ms",
        "max_ms",
        "peak_delta_kb",
        "rss_baseline_kb",
        "rss_after_kb",
        "cpu_percent_sample",
    }
    for r in data["results"]:
        missing = required - set(r)
        assert not missing, (
            f"cell {r.get('strategy')!r}/{r.get('tier')!r} missing fields: {sorted(missing)}"
        )


def test_p95_at_or_above_p50() -> None:
    """Sanity gate: p95 cannot be below p50. Catches a sort / percentile
    regression in the harness."""
    data = _load_baseline()
    for r in data["results"]:
        if r["iterations"] == 0:
            continue  # error path, no timing collected
        assert r["p95_ms"] >= r["p50_ms"], (
            f"p95 < p50 on {r['strategy']!r}/{r['tier']!r}: "
            f"p50={r['p50_ms']} p95={r['p95_ms']}"
        )
