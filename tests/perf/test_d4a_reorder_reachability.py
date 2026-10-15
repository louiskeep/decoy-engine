"""D4a: the external-reorder lane is reachable through `run_pipeline` on an FK chain
with a resolved memory + disk budget, actually spills and merges, and stays bounded.

Plan: docs/plans/2026-10-15-d4-scale-proof.md section 2. The R3 test
(`tests/unit/execution/test_r3_ooc_reorder_reachability.py`) proves the ROUTE DECISION
on 20-row tables; this proves the lane DOES ITS JOB at a size that forces real spill.
It is the moderate-scale gate before any 100M spend: if it fails, the finding is D4b
(fix the lane), not a bench problem.

The 2M parent-key production threshold is lowered through the public
`out_of_core_reorder_threshold_rows` knob so the witness stays seconds, not minutes;
the same worker is run at the REAL threshold for the recorded evidence
(docs/records/2026-10-15-d4a-reorder-revalidation.md). A threshold override changes only
which route a table takes, never its output (`_route_policy.py`), so the lane exercised
here is the production lane.

Evidence per run (`tests/perf_fixtures/ooc_route_witness.py`): which driver ran per
table, how many sorter runs hit disk, how many k-way merges ran, and each sorter's
resident peak against `run_bytes_cap * SORT_OVERHEAD_FACTOR`. Constructing the sorter is
not evidence; writing runs and merging them is.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.perf

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts" / "fk-scale"
sys.path.insert(0, str(_SCRIPTS))

import build_fk_chain  # noqa: E402

# Parent keys (60k) clear the lowered threshold; child and grandchild are sized so each
# edge's slim sort volume is a few times the 128 MiB budget's run_bytes_cap (~20 MB), which
# forces >= 2 initial runs and therefore a merge. Below ~128 MiB the DuckDB join phase
# itself runs out of memory (measured), so this is the smallest budget that completes.
_COUNTS = {"parent": 60_000, "child": 420_000, "grandchild": 420_000}
_THRESHOLD = 50_000
_BUDGET_MB = 128
# External RSS guard. Interpreter + Arrow + DuckDB baseline is ~210 MB at tiny N, so this
# budget is dominated by fixed cost; the guard catches runaway growth, and the 1.35x-of-
# ceiling bar is checked on the real-threshold run (record), where the budget dwarfs it.
_RSS_GUARD_MB = 2 * _BUDGET_MB + 256


def _run_worker(data_dir: Path, work_root: Path, *extra: str) -> dict:
    proc = subprocess.run(  # noqa: S603 -- fixed in-repo script, test-controlled args
        [
            sys.executable,
            str(_SCRIPTS / "bench_worker_fk.py"),
            "--data-dir",
            str(data_dir),
            "--work-root",
            str(work_root),
            "--budget-mb",
            str(_BUDGET_MB),
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH_JSON "))
    return json.loads(line.removeprefix("BENCH_JSON "))


@pytest.fixture(scope="module")
def chain(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("d4a-chain")
    build_fk_chain.build(out, _COUNTS, width=2, orphan_frac=0.01)
    return out


def test_reorder_taken_spills_merges_and_stays_bounded(chain: Path, tmp_path: Path) -> None:
    rec = _run_worker(
        chain, tmp_path, "--reorder", str(_THRESHOLD), "--rss-ceiling-mb", str(_RSS_GUARD_MB)
    )

    assert rec["completed"], rec["failure"]
    assert rec["route"]["execution_mode"] == "out_of_core"
    # Reorder is taken for both FK children, never for the root table.
    assert rec["reorder"]["driver_by_table"] == {
        "parent": "batch",
        "child": "reorder",
        "grandchild": "reorder",
    }
    spill = rec["spill"]
    assert spill["sorters"] == 2
    # Real spill + merge on every edge, not merely a constructed sorter.
    for sorter in spill["detail"]:
        assert sorter["runs_written"] >= 2
        assert sorter["merge_groups"] >= 1
        assert sorter["spilled_bytes"] > 0
        resident = max(sorter["peak_buffered_bytes"], sorter["peak_merge_resident_bytes"])
        assert 0 < resident <= sorter["resident_envelope_bytes"]
    assert spill["temp_disk_peak_mb"] > 0
    assert rec["rss_within_ceiling"], rec["peak_rss_mb"]
    # The output is right, not just bounded: row counts and FK parity on both edges.
    assert rec["rowcount_match"]
    assert rec["parity"]["status"] == "ok"
    assert set(rec["parity"]["edges"]) == {"parent->child", "child->grandchild"}


def test_batch_joiner_control_does_not_reorder(chain: Path, tmp_path: Path) -> None:
    """Anti-vacuity: the same chain with reorder OFF takes the batch route and spills no
    sorter runs, so the spill/merge evidence above is attributable to the reorder lane."""
    rec = _run_worker(chain, tmp_path, "--reorder", "off")
    assert rec["completed"], rec["failure"]
    assert not rec["reorder"]["taken"]
    assert set(rec["reorder"]["driver_by_table"].values()) == {"batch"}
    assert rec["spill"]["runs_written"] == 0 and rec["spill"]["merge_groups"] == 0
    assert rec["parity"]["status"] == "ok"
