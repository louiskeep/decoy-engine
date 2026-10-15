"""GAP9 T4 (scaling, informational) and T5 (out-of-core memory stays bounded at N threads).

Plan: `docs/plans/2026-10-15-gap9-native-thread-grant.md` section 5. Each measurement runs
`scripts/fk-scale/bench_worker_fk.py` in a FRESH process (the external-RSS harness the D4
reorder proofs use), once at `native_threads=1` and once at N, on a lazy source with an
incremental sink. Scaled devbox proxy: no GCP, no 100M run.

T4 records the wall-time delta and the effective parallelism (child CPU seconds over wall
seconds) and never fails on speed: the rayon pool sizes from host cores and the engine clamps
the grant to the host and the thread knee, so a noisy or clamped-to-1 result is a valid result.

T5 asserts what must hold at any thread count: the same driver per table, the same output
(row counts and windowed positional FK parity), the same native column set, and a peak RSS
within a stated tolerance of the single-thread run. The tolerance is additive, not
multiplicative: the rayon pool is sized from host cores, not from the grant, so what N adds is
per-thread batch buffers, bounded by the batch size and not by the table.
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.perf

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts" / "fk-scale"
sys.path.insert(0, str(_SCRIPTS))

import build_fk_chain  # noqa: E402

_THREADS = 4
_BUDGET_MB = 128
_THRESHOLD = 50_000
# Peak RSS at N may exceed the single-thread peak by at most this much. Measured growth on this
# fixture is far below it; the bar is generous enough to absorb allocator noise and tight enough
# that per-thread whole-table buffers (tens to hundreds of MB here) would trip it.
_RSS_DELTA_TOLERANCE_MB = 128
# The same absolute ceilings the single-thread proofs use, plus the tolerance above.
_MODERATE = {
    "counts": {"parent": 60_000, "child": 420_000, "grandchild": 420_000},
    "drivers": {"parent": "batch", "child": "reorder", "grandchild": "reorder"},
    "rss_bar_mb": 2 * _BUDGET_MB + 256 + _RSS_DELTA_TOLERANCE_MB,
}
_DEMOTED = {
    "counts": {"parent": 60_000, "child": 600_000, "grandchild": 600_000},
    "drivers": {"parent": "batch", "child": "reorder", "grandchild": "batch"},
    "rss_bar_mb": 560 + _RSS_DELTA_TOLERANCE_MB,
}


def _worker(data_dir: Path, work_root: Path, threads: int, *extra: str) -> dict[str, Any]:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
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
            "--native-threads",
            str(threads),
            "--timeout-s",
            "600",
            *extra,
        ],
        capture_output=True,
        text=True,
        timeout=660,
        check=False,
    )
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH_JSON "))
    record: dict[str, Any] = json.loads(line.removeprefix("BENCH_JSON "))
    cpu_s = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
    mask_s = record["mask_s"] or 0.0
    # Process CPU over the masking wall time overstates parallelism a little (it includes
    # startup and verification), so this is a recorded signal, not a bar.
    record["effective_parallelism"] = round(cpu_s / mask_s, 2) if mask_s else None
    return record


def _native_columns(record: dict[str, Any]) -> int:
    return record["disposition"]["native_columns"]


@pytest.fixture(scope="module")
def moderate_chain(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("gap9-moderate")
    build_fk_chain.build(out, _MODERATE["counts"], width=2, orphan_frac=0.01)
    return out


@pytest.fixture(scope="module")
def demoted_chain(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("gap9-demoted")
    build_fk_chain.build(out, _DEMOTED["counts"], width=2, orphan_frac=0.01)
    return out


@pytest.mark.parametrize("shape", ["moderate", "demoted"])
def test_t5_out_of_core_memory_is_bounded_at_n_threads(
    shape: str,
    moderate_chain: Path,
    demoted_chain: Path,
    tmp_path: Path,
    record_property: Any,
) -> None:
    spec, chain = (_MODERATE, moderate_chain) if shape == "moderate" else (_DEMOTED, demoted_chain)
    runs = {
        n: _worker(chain, tmp_path, n, "--reorder", str(_THRESHOLD), "--rss-ceiling-mb", "4096")
        for n in (1, _THREADS)
    }
    for n, rec in runs.items():
        assert rec["completed"], (n, rec["failure"])
        assert rec["route"]["execution_mode"] == "out_of_core"
        assert rec["requested"]["native_threads"] == n
        # Driver selection (and so guard demotion) is identical at 1 and N.
        assert rec["reorder"]["driver_by_table"] == spec["drivers"], n
        # Same output at both thread counts: complete, with FK links intact.
        assert rec["rowcount_match"] and rec["parity"]["status"] == "ok", n
        # Native evidence: the compiled kernels ran for the hashed columns.
        assert _native_columns(rec) > 0 and not rec["disposition"]["non_native"], n
    assert _native_columns(runs[1]) == _native_columns(runs[_THREADS])
    assert runs[1]["rows"] == runs[_THREADS]["rows"]
    assert set(runs[1]["parity"]["edges"]) == set(runs[_THREADS]["parity"]["edges"])

    one, many = runs[1]["peak_rss_mb"], runs[_THREADS]["peak_rss_mb"]
    summary = {
        "shape": shape,
        "driver_by_table": spec["drivers"],
        "peak_rss_mb": {"1": one, str(_THREADS): many},
        "rss_delta_mb": round(many - one, 1),
        "tolerance_mb": _RSS_DELTA_TOLERANCE_MB,
        "absolute_bar_mb": spec["rss_bar_mb"],
        "effective_parallelism": {str(n): r["effective_parallelism"] for n, r in runs.items()},
        "cpu_count": runs[1]["requested"]["cpu_count"],
    }
    record_property("gap9_t5", json.dumps(summary))
    print("GAP9_T5", json.dumps(summary))
    assert many <= one + _RSS_DELTA_TOLERANCE_MB, summary
    assert many <= spec["rss_bar_mb"], summary


def test_t4_scaling_is_recorded_not_enforced(
    moderate_chain: Path, tmp_path: Path, record_property: Any
) -> None:
    """INFORMATIONAL. Completes and matches at both thread counts; the speedup is only recorded."""
    runs = {n: _worker(moderate_chain, tmp_path, n, "--reorder", "off") for n in (1, _THREADS)}
    for n, rec in runs.items():
        assert rec["completed"], (n, rec["failure"])
        assert rec["rowcount_match"] and rec["parity"]["status"] == "ok", n
    one, many = runs[1]["mask_s"], runs[_THREADS]["mask_s"]
    summary = {
        "mask_s": {"1": one, str(_THREADS): many},
        "speedup": round(one / many, 2) if one and many else None,
        "effective_parallelism": {str(n): r["effective_parallelism"] for n, r in runs.items()},
        "cpu_count": runs[1]["requested"]["cpu_count"],
        "note": "informational; a clamped or noisy result does not fail",
    }
    record_property("gap9_t4", json.dumps(summary))
    print("GAP9_T4", json.dumps(summary))
