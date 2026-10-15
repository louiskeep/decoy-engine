"""T6 (`docs/plans/2026-10-15-reorder-parent-size-guard.md` section 5): the parent-size
guard demotes a real over-ceiling parent to the batch lane in a fresh subprocess,
through the production path (`run_pipeline` -> `run_fk_out_of_core` -> `decide_route`).

Scaled devbox proxy, no GCP, no 100M run. At a 128 MiB budget the guard's ceiling is
466_666 distinct parent keys (`_reorder_budget.safe_parent_key_ceiling`). The chain's
`child` table (600k rows) is the grandchild's parent, so that edge is genuinely over
the ceiling, while the `parent` -> `child` edge (60k keys, over the lowered 50k selection
threshold) is under it. `test_d4a_reorder_reachability.py` is the control: the same
shape with a 420k-row `child` keeps BOTH children on reorder.

The fixture is generated here, outside the measured worker. The worker runs with an
explicit 128 MiB budget, a 600 s timeout, and the default disk budget.

RSS bar: 560 MB external peak. Measured on this fixture: ~432 MB (interpreter + Arrow +
DuckDB baseline dominates at this scale). The bar is this fixture's batch envelope with
~30% headroom, not the ~3.9 GB 35M-key envelope and not a universal boundedness claim.
Order is checked by the worker's windowed positional parity (`_parity._check_edge` compares
source and masked rows at the same row offsets and fails on a broken link).
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

_COUNTS = {"parent": 60_000, "child": 600_000, "grandchild": 600_000}
_THRESHOLD = 50_000
_BUDGET_MB = 128
_CEILING_AT_BUDGET = 466_666
_RSS_BAR_MB = 560


def test_over_ceiling_parent_demotes_to_batch_and_stays_bounded(tmp_path: Path) -> None:
    assert _COUNTS["child"] > _CEILING_AT_BUDGET > _COUNTS["parent"]
    data_dir = tmp_path / "chain"
    build_fk_chain.build(data_dir, _COUNTS, width=2, orphan_frac=0.01)

    proc = subprocess.run(  # noqa: S603 -- fixed in-repo script, test-controlled args
        [
            sys.executable,
            str(_SCRIPTS / "bench_worker_fk.py"),
            "--data-dir",
            str(data_dir),
            "--work-root",
            str(tmp_path),
            "--budget-mb",
            str(_BUDGET_MB),
            "--reorder",
            str(_THRESHOLD),
            "--rss-ceiling-mb",
            str(_RSS_BAR_MB),
            "--timeout-s",
            "600",
        ],
        capture_output=True,
        text=True,
        timeout=660,
        check=False,
    )
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH_JSON "))
    rec = json.loads(line.removeprefix("BENCH_JSON "))

    assert rec["completed"], rec["failure"]
    assert rec["route"]["execution_mode"] == "out_of_core"
    # Demotion witness: the under-ceiling edge reorders, the over-ceiling one is batch.
    assert rec["reorder"]["driver_by_table"] == {
        "parent": "batch",
        "child": "reorder",
        "grandchild": "batch",
    }
    # Exactly one sorter (the child edge): the grandchild never built a reorder lane.
    assert rec["spill"]["sorters"] == 1
    assert rec["rss_within_ceiling"], rec["peak_rss_mb"]
    # Completeness and order: all rows out, FK links intact at sampled row offsets.
    assert rec["rowcount_match"]
    assert rec["parity"]["status"] == "ok"
    assert set(rec["parity"]["edges"]) == {"parent->child", "child->grandchild"}
