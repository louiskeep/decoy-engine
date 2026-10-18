"""F4 opt-in scale tiers for the mixed generate + mask route (1M and 10M total rows).

Plan: docs/plans/2026-10-17-phase-f-f4-capacity.md sections 3 and 4. Excluded from the
default suite (`benchmark`); run with `-m benchmark`. Tiers are exact two-table counts:
1M = parent 0.2M / child 0.8M, 10M = parent 2M / child 8M. 100M (20M / 80M) is deferred.

Capacity runs and the oracle byte-identity check are separate processes: the oracle
materializes the whole output and would dominate RSS. Oracle matrix: 1M here, 10M is
capacity-only. The 8 GiB ceiling is a PROPOSED mixed-run ceiling, not a proven bound
(resident generation and child-key memory grow with cardinality).
"""

from __future__ import annotations

import importlib.util

import pytest

from tests.perf._f4_mixed_runner import run_worker

pytestmark = [
    pytest.mark.benchmark,
    pytest.mark.skipif(
        importlib.util.find_spec("decoy_engine_native") is None,
        reason="decoy-engine-native companion not installed",
    ),
]

CEILING_MB = 8192
TIERS = {"1M": (200_000, 800_000), "10M": (2_000_000, 8_000_000)}


def _rows(tier: str) -> list[str]:
    parents, children = TIERS[tier]
    return ["--parent-rows", str(parents), "--child-rows", str(children)]


@pytest.mark.parametrize("tier", list(TIERS))
def test_mixed_capacity_tier(tier: str) -> None:
    record = run_worker([*_rows(tier), "--rss-ceiling-mb", str(CEILING_MB)], timeout_s=1800)
    assert record["completed"] is True, record["failure"]  # a killed run is never "bounded"
    assert record["rss_within_ceiling"] is True, record["peak_rss_mb"]
    route = record["route"]
    assert route["ok"], route["checks"]
    assert route["disposition"]["chunk_count"] > 1
    assert record["fk_parity"]["status"] == "ok", record["fk_parity"]["problems"]
    assert record["fk_parity"]["fk_full_scan"]["mismatches"] == 0
    print(f"BENCH_TIER {tier} peak_rss_mb={record['peak_rss_mb']} mask_s={record['mask_s']}")


def test_mixed_oracle_byte_identity_1m() -> None:
    record = run_worker(["--mode", "oracle", *_rows("1M")], timeout_s=1800)
    assert record["completed"] is True, record["failure"]
    assert record["oracle"]["byte_identical"] is True, record["oracle"]["mismatch"]


# Observed 1418-1420 MB over 3 runs after the Arrow-native string write-back
# (2026-10-18); before it 1906 MB (to_pylist) and 2009 MB (baseline). The ceiling is
# the observed peak plus a small margin, so a regression back to a Python-list
# write-back fails here.
FK_WRITEBACK_10M_CEILING_MB = 1500


def test_mixed_10m_string_fk_writeback_peak_rss() -> None:
    record = run_worker(
        [*_rows("10M"), "--rss-ceiling-mb", str(FK_WRITEBACK_10M_CEILING_MB)], timeout_s=1800
    )
    assert record["completed"] is True, record["failure"]
    assert record["peak_rss_mb"] <= FK_WRITEBACK_10M_CEILING_MB, record["peak_rss_mb"]
    assert record["fk_parity"]["status"] == "ok", record["fk_parity"]["problems"]
