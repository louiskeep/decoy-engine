"""Phase F F4a: lower-tier (1M / 10M) generation and mixed-job capacity evidence.

Plan: docs/plans/2026-10-16-phase-f-program.md section F4, prerequisite (a). Opt-in
(`-m benchmark`). The 100M tier and the platform evidence transport are separate slices.

Four claims, each from OUTSIDE the measured process where it matters:

1. Pure generation (representative plan: sequence, reference, categorical, null_mask, pooled
   faker, windowed_date) streams into a Parquet sink under one byte budget, with external peak RSS
   (`wait4` ru_maxrss) under a ceiling declared BELOW, before any 10M run, and flat from 1M to 10M.
2. A mixed generate-then-mask job (rich generated parent, masked child over one FK edge, the
   production `run_pipeline` mixed route) holds the same two properties.
3. Reproducibility: a second run of the same configuration commits an identical content digest.
4. Per-column executed backend, from the F0 witness over the same table definitions at a row count
   above the pooled-faker threshold. Backend choice follows the plan, not the row count.

Ceilings are predeclared and are not raised to make a tier pass; a breach is a finding.
`F4A_RESULTS_PATH` writes the combined record (used to produce the committed evidence file).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.benchmark

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "fk-scale"
sys.path.insert(0, str(_SCRIPTS))

import bench_worker_gen  # noqa: E402
import bench_worker_mixed  # noqa: E402

TIERS = {"1M": 1_000_000, "10M": 10_000_000}
BUDGET_MB = 1024

# Predeclared absolute external-RSS ceilings, set from smokes at 1M and before any 10M run. The FR0
# analog peaked near 210 MB and a 1M smoke of this plan near 211 MB (a 175 MB interpreter + Arrow
# baseline); 500 MB is about 2.4x. A 1M smoke of the mixed route peaked near 860 MB; 1500 MB is
# about 1.7x. The ceilings were not changed after the 10M results.
GEN_CEILING_MB = 500
MIXED_CEILING_MB = 1500
# Allocator slack on top of the allowed growth.
MAX_TIER_GROWTH = 1.15
# Pure generation is bounded, not merely linear: 10x rows must not move the peak beyond noise.
MAX_FLAT_GROWTH = 1.25

MIXED_PARENT_ROWS = 200_000  # fixed across tiers; above the 50k pooled-faker threshold
TIMEOUT_S = 7200

# Expected executed backend per generated column on this branch (FR3 kernel selection is shelved,
# so no pooled or RNG selection runs on the Rust companion). A change here is a change in
# where generation runs and must be a deliberate edit.
EXPECTED_SELECTION = {
    "sequence": {"no_selection"},
    "reference": {"python_rng_selection"},
    "categorical": {"python_rng_selection"},
    "faker": {"numpy_selection"},
}
ALLOWED_SUBPATHS = {"pool_construction", "null_mask"}


def _gen(rows: int, mode: str = "capacity", plan: str = "rep") -> dict[str, Any]:
    args = bench_worker_gen._parser().parse_args(
        [
            "--rows", str(rows),
            "--plan", plan,
            "--mode", mode,
            "--budget-mb", str(BUDGET_MB),
            "--rss-ceiling-mb", str(GEN_CEILING_MB),
            "--rss-kill-mb", str(GEN_CEILING_MB * 2),
            "--timeout-s", str(TIMEOUT_S),
        ]
    )  # fmt: skip
    return bench_worker_gen.supervise(args)


def _mixed(child_rows: int) -> dict[str, Any]:
    args = bench_worker_mixed._parser().parse_args(
        [
            "--parent-rows", str(MIXED_PARENT_ROWS),
            "--child-rows", str(child_rows),
            "--rich-parent",
            "--rss-ceiling-mb", str(MIXED_CEILING_MB),
            "--rss-kill-mb", str(MIXED_CEILING_MB * 2),
            "--timeout-s", str(TIMEOUT_S),
        ]
    )  # fmt: skip
    return bench_worker_mixed.supervise(args)


@pytest.fixture(scope="module")
def evidence() -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": "f4a-capacity-evidence/v1",
        "ceilings_mb": {"generation": GEN_CEILING_MB, "mixed": MIXED_CEILING_MB},
        "max_tier_growth": MAX_TIER_GROWTH,
        "budget_mb": BUDGET_MB,
        "mixed_parent_rows": MIXED_PARENT_ROWS,
        "generation": {},
        "mixed": {},
        "backends": {
            "generation_plan": _gen(100_000, mode="probe"),
            "mixed_parent": _gen(MIXED_PARENT_ROWS, mode="probe", plan="mixed-parent"),
        },
    }
    for tier, rows in TIERS.items():
        record["generation"][tier] = {"run1": _gen(rows), "run2": _gen(rows)}
        record["mixed"][tier] = {"run1": _mixed(rows), "run2": _mixed(rows)}
    path = os.environ.get("F4A_RESULTS_PATH")
    if path:
        Path(path).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    return record


@pytest.mark.parametrize("tier", list(TIERS))
def test_generation_streams_under_the_ceiling(tier: str, evidence: dict[str, Any]) -> None:
    for run in evidence["generation"][tier].values():
        assert run["completed"] is True, run["failure"]
        result = run["result"]
        assert result["mode"] == "streamed", result["reason"]
        assert (
            result["committed_rows"]
            == result["expected_rows"]
            == {
                "orders": TIERS[tier],
                "people": bench_worker_gen.PARENT_ROWS,
            }
        )
        assert run["rss_within_ceiling"] is True, run["peak_rss_mb"]


@pytest.mark.parametrize("tier", list(TIERS))
def test_mixed_job_completes_under_the_ceiling(tier: str, evidence: dict[str, Any]) -> None:
    for run in evidence["mixed"][tier].values():
        assert run["completed"] is True, run["failure"]
        assert run["route"]["ok"] is True, run["route"]["checks"]
        assert run["fk_parity"]["status"] == "ok", run["fk_parity"]["problems"]
        assert run["rss_within_ceiling"] is True, run["peak_rss_mb"]


def _baseline(run: dict[str, Any], family: str) -> float:
    """The worker's RSS after imports and before the job: interpreter plus Arrow, not the job."""
    key = run["result"]["rss_before_mb"] if family == "generation" else run["rss_before_run_mb"]
    return float(key)


@pytest.mark.parametrize("family", ["generation", "mixed"])
def test_rss_growth_is_not_superlinear(family: str, evidence: dict[str, Any]) -> None:
    """The job's own memory (peak minus the pre-job baseline) must not grow faster than its rows.

    10x the rows may cost at most 10x the job memory, plus allocator slack. This is the bound the
    program plan sets for the 1M -> 10M step; it is weaker than flat on purpose, because the mixed
    route's FK carrier holds child keys resident and so scales with child rows by design.
    """
    small = list(evidence[family]["1M"].values())
    large = list(evidence[family]["10M"].values())
    base = _baseline(small[0], family)
    job_small = max(r["peak_rss_mb"] for r in small) - base
    job_large = max(r["peak_rss_mb"] for r in large) - base
    ratio = TIERS["10M"] / TIERS["1M"]
    assert job_large <= job_small * ratio * MAX_TIER_GROWTH, (
        f"{family}: job memory {job_small:.0f} MB at 1M -> {job_large:.0f} MB at 10M"
    )


def test_generation_rss_is_flat_between_tiers(evidence: dict[str, Any]) -> None:
    """Pure generation streams through the bounded sink, so its peak must not track rows at all."""
    small = max(r["peak_rss_mb"] for r in evidence["generation"]["1M"].values())
    large = max(r["peak_rss_mb"] for r in evidence["generation"]["10M"].values())
    assert large <= small * MAX_FLAT_GROWTH, f"RSS grew: 1M={small} MB, 10M={large} MB"


@pytest.mark.parametrize("family", ["generation", "mixed"])
@pytest.mark.parametrize("tier", list(TIERS))
def test_rerun_commits_identical_content(family: str, tier: str, evidence: dict[str, Any]) -> None:
    def digest(run: dict[str, Any]) -> str:
        d = run["digest"]
        assert d, "run produced no digest"
        return str(d["combined"])

    runs = evidence[family][tier]
    assert digest(runs["run1"]) == digest(runs["run2"])


@pytest.mark.parametrize("which", ["generation_plan", "mixed_parent"])
def test_per_column_backend_is_recorded_and_as_expected(
    which: str, evidence: dict[str, Any]
) -> None:
    probe = evidence["backends"][which]
    assert probe["completed"] is True, probe["failure"]
    result = probe["result"]
    assert result["core_eligible"] is True
    assert result["columns"], "no per-column backend record"
    for col in result["columns"]:
        if col["kind"] == "windowed_date":
            assert col["admitted_to_core"] is True
            assert col["populated_rows"] > 0
            continue
        assert set(col["selection"]) == EXPECTED_SELECTION[col["kind"]], col
        assert set(col["subpaths"]) <= ALLOWED_SUBPATHS, col
        assert ("pool_construction" in col["subpaths"]) == (col["kind"] == "faker"), col
