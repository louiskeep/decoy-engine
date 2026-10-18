"""Phase F FR0: bounded generation holds a flat external RSS at 5M and 10M rows.

Plan: docs/plans/2026-10-18-phase-f-rebaseline-f1-f3.md section 6. Opt-in (`-m benchmark`), like the
F4 capacity tiers. `scripts/fk-scale/bench_worker_gen.py` streams a parent/child plan (a pooled
Faker column on each table and a reference over a fixed 100k-key parent) into a
`ParquetTransactionalSink` under ONE byte budget and ONE frozen batch size, and the supervisor reads
peak RSS from OUTSIDE the worker (`wait4` ru_maxrss). The test is non-vacuous in three ways: the
run must really stream (mode `streamed`, committed rows equal the request), the peak must sit
under a predeclared ceiling, and the 10M peak must not grow materially over the 5M peak (a
resident run would grow by gigabytes).

The RSS kill in the supervisor is bench-only containment. The memory guarantee itself is the
ledger's reserve-before-allocate refusal, asserted in-process in
tests/unit/generation/test_fr0_budget.py.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.benchmark

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "fk-scale"
sys.path.insert(0, str(_SCRIPTS))

import bench_worker_gen  # noqa: E402

BUDGET_MB = 1024
# Predeclared with about 2x headroom over the observed ~210 MB peak (a 175 MB interpreter + Arrow
# baseline). A resident run holds the whole output in RAM, so it grows with rows.
CEILING_MB = 400
# A flat-memory run does not grow with rows; allow noise and allocator slack only.
MAX_TIER_GROWTH = 1.15
TIERS = {"5M": 5_000_000, "10M": 10_000_000}


def _run(rows: int) -> dict[str, Any]:
    args = bench_worker_gen._parser().parse_args(
        [
            "--rows", str(rows),
            "--budget-mb", str(BUDGET_MB),
            "--rss-ceiling-mb", str(CEILING_MB),
            "--rss-kill-mb", str(CEILING_MB * 2),
            "--timeout-s", "1800",
        ]
    )  # fmt: skip
    return bench_worker_gen.supervise(args)


@pytest.fixture(scope="module")
def records() -> dict[str, dict[str, Any]]:
    return {tier: _run(rows) for tier, rows in TIERS.items()}


@pytest.mark.parametrize("tier", list(TIERS))
def test_tier_streams_under_the_ceiling(tier: str, records: dict[str, dict[str, Any]]) -> None:
    record = records[tier]
    assert record["completed"] is True, record["failure"]
    result = record["result"]
    assert result["mode"] == "streamed", result["reason"]
    assert (
        result["committed_rows"]
        == result["expected_rows"]
        == {
            "orders": TIERS[tier],
            "people": 100_000,
        }
    )
    assert record["rss_within_ceiling"] is True, record["peak_rss_mb"]
    print(f"FR0_TIER {tier} peak_rss_mb={record['peak_rss_mb']} wall_s={record['wall_s']}")


def test_rss_does_not_grow_between_tiers(records: dict[str, dict[str, Any]]) -> None:
    small, large = records["5M"]["peak_rss_mb"], records["10M"]["peak_rss_mb"]
    assert large <= small * MAX_TIER_GROWTH, f"RSS grew with rows: 5M={small} MB, 10M={large} MB"
