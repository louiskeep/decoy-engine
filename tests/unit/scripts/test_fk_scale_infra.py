"""D4 scale-proof infrastructure: fixture builder, external supervisor, FK worker.

Plan: docs/plans/2026-10-15-d4-scale-proof.md section 6. Small-N correctness of the
tooling that the 100M/200M runs depend on; the runs themselves are evidence, not tests.
The worker is exercised through its real CLI in a subprocess (it measures a child
process), so these also prove the supervisor contract: rc=0 is never success, and a
killed, hung or silent worker still yields a record.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts" / "fk-scale"
sys.path.insert(0, str(_SCRIPTS))

import _chain_layout as layout  # noqa: E402
import _parity  # noqa: E402
import _supervisor as sup  # noqa: E402
import build_fk_chain  # noqa: E402

_COUNTS = {"parent": 1_000, "child": 1_800, "grandchild": 2_500}


# ------------------------------------------------------------------ fixture builder


@pytest.fixture(scope="module")
def chain(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return build_fk_chain.build(
        tmp_path_factory.mktemp("chain"), _COUNTS, width=3, orphan_frac=0.0, chunk_rows=300
    )


def test_builder_writes_the_declared_asymmetric_counts(chain: dict[str, Path]) -> None:
    assert {t: _parity.table_rows(p) for t, p in chain.items()} == _COUNTS


def test_builder_fk_references_resolve_and_cover_the_parent(chain: dict[str, Path]) -> None:
    parent = set(pq.read_table(chain["parent"], columns=["id"]).column(0).to_pylist())
    child = pq.read_table(chain["child"], columns=["id", "parent_id"])
    grand = set(pq.read_table(chain["grandchild"], columns=["child_id"]).column(0).to_pylist())
    assert set(child.column("parent_id").to_pylist()) <= parent  # RI by construction
    assert set(child.column("parent_id").to_pylist()) == parent  # child > parent: every parent used
    assert grand <= set(child.column("id").to_pylist())


def test_builder_writes_chunk_by_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No table is built whole: every write is at most one chunk, one row group each."""
    sizes: list[int] = []
    real = pq.ParquetWriter.write_table

    def spy(self: pq.ParquetWriter, table: pa.Table, *a: object, **k: object) -> None:
        sizes.append(table.num_rows)
        real(self, table, *a, **k)

    monkeypatch.setattr(pq.ParquetWriter, "write_table", spy)
    paths = build_fk_chain.build(tmp_path, _COUNTS, width=1, chunk_rows=300)
    assert max(sizes) <= 300 and sum(sizes) == sum(_COUNTS.values())
    assert pq.ParquetFile(paths["grandchild"]).metadata.num_row_groups == 9  # ceil(2500/300)


def test_builder_is_deterministic_and_plants_orphans(tmp_path: Path) -> None:
    a = build_fk_chain.build(tmp_path / "a", _COUNTS, width=2, orphan_frac=0.05, seed=3)
    b = build_fk_chain.build(tmp_path / "b", _COUNTS, width=2, orphan_frac=0.05, seed=3)
    assert pq.read_table(a["child"]).equals(pq.read_table(b["child"]))
    fks = pq.read_table(a["child"], columns=["parent_id"]).column(0).to_pylist()
    assert sum(v.startswith("orphan_p") for v in fks) == _COUNTS["child"] // 20


def test_builder_width_gives_about_fifteen_columns(chain: dict[str, Path]) -> None:
    assert len(pq.ParquetFile(chain["child"]).schema_arrow.names) == 2 + 3
    default = build_fk_chain.DEFAULT_WIDTH
    assert 14 <= default + 2 <= 16


# ------------------------------------------------------------------------- parity


def _masked_copy(chain: dict[str, Path], out: Path, *, break_child_link: bool = False) -> None:
    """A stand-in for a correct run: every key and FK mapped through one consistent function."""
    out.mkdir()

    def mask(col: pa.ChunkedArray) -> pa.ChunkedArray:
        return pc.binary_join_element_wise(pa.scalar("m_"), col, pa.scalar(""))

    for table, path in chain.items():
        t = pq.read_table(path)
        for col in [c for c in ("id", "parent_id", "child_id") if c in t.column_names]:
            t = t.set_column(t.column_names.index(col), col, mask(t.column(col)))
        if break_child_link and table == "child":
            bad = t.column("parent_id").to_pylist()
            bad[1] = "tampered"
            t = t.set_column(t.column_names.index("parent_id"), "parent_id", pa.array(bad))
        pq.write_table(t, out / f"{table}.parquet", row_group_size=300)


def test_parity_accepts_a_consistent_masking(chain: dict[str, Path], tmp_path: Path) -> None:
    _masked_copy(chain, tmp_path / "ok")
    verdict = _parity.verify_committed_chain(chain, tmp_path / "ok", sample=200)
    assert verdict["rowcount_match"] and verdict["parity"]["status"] == "ok"
    assert set(verdict["parity"]["edges"]) == {"parent->child", "child->grandchild"}


def test_parity_rejects_a_broken_link(chain: dict[str, Path], tmp_path: Path) -> None:
    _masked_copy(chain, tmp_path / "bad", break_child_link=True)
    verdict = _parity.verify_committed_chain(chain, tmp_path / "bad", sample=200)
    assert verdict["parity"]["status"] == "MISMATCH"


def test_parity_rejects_unmasked_keys_and_row_count_drift(
    chain: dict[str, Path], tmp_path: Path
) -> None:
    out = tmp_path / "same"
    out.mkdir()
    for table, path in chain.items():
        pq.write_table(pq.read_table(path), out / f"{table}.parquet")
    assert _parity.verify_committed_chain(chain, out)["parity"]["status"] == "MISMATCH"
    short = tmp_path / "short"
    short.mkdir()
    for table, path in chain.items():
        pq.write_table(pq.read_table(path).slice(0, 10), short / f"{table}.parquet")
    assert not _parity.verify_committed_chain(chain, short)["rowcount_match"]


def test_read_window_wraps_past_the_end(chain: dict[str, Path]) -> None:
    got = layout.read_window(chain["parent"], "id", _COUNTS["parent"] - 2, 4)
    assert got == ["p998", "p999", "p0", "p1"]


# ------------------------------------------------------------------------ supervisor


def _supervise(code: str, tmp_path: Path, **kw: object) -> sup.WorkerOutcome:
    result = tmp_path / "result.json"
    body = code.replace("RESULT", repr(str(result)))
    return sup.run_worker(
        [sys.executable, "-c", body],
        env={"PATH": "/usr/bin"},
        result_path=result,
        timeout_s=kw.get("timeout_s"),
        rss_kill_mb=kw.get("rss_kill_mb"),
    )


def test_supervisor_reports_a_killed_worker(tmp_path: Path) -> None:
    out = _supervise("import os,signal;os.kill(os.getpid(),signal.SIGKILL)", tmp_path)
    failure = sup.classify_failure(out)
    assert out.term_signal == signal.SIGKILL
    assert failure is not None and failure["kind"] == "killed_sigkill_probable_oom"
    assert out.peak_rss_mb > 0  # the kernel still reports the dead worker's peak


def test_supervisor_rc_zero_without_a_result_is_not_success(tmp_path: Path) -> None:
    out = _supervise("pass", tmp_path)
    assert out.returncode == 0
    failure = sup.classify_failure(out)
    assert failure is not None and failure["kind"] == "result_missing"


def test_supervisor_malformed_result_is_not_success(tmp_path: Path) -> None:
    out = _supervise("open(RESULT,'w').write('{nope')", tmp_path)
    failure = sup.classify_failure(out)
    assert failure is not None and failure["kind"] == "result_malformed"


def test_supervisor_requires_the_worker_to_claim_completion(tmp_path: Path) -> None:
    out = _supervise("import json;json.dump({'completed_inner':False},open(RESULT,'w'))", tmp_path)
    failure = sup.classify_failure(out)
    assert failure is not None and failure["kind"] == "worker_reported_failure"
    ok = _supervise("import json;json.dump({'completed_inner':True},open(RESULT,'w'))", tmp_path)
    assert sup.classify_failure(ok) is None


def test_supervisor_nonzero_exit_after_a_good_result_still_fails(tmp_path: Path) -> None:
    out = _supervise(
        "import json,sys;json.dump({'completed_inner':True},open(RESULT,'w'));sys.exit(5)",
        tmp_path,
    )
    failure = sup.classify_failure(out)
    assert failure is not None and failure["kind"] == "nonzero_exit_after_result"


def test_supervisor_times_out_a_hung_worker(tmp_path: Path) -> None:
    out = _supervise("import time;time.sleep(60)", tmp_path, timeout_s=0.6)
    failure = sup.classify_failure(out)
    assert out.timed_out and failure is not None and failure["kind"] == "timeout"
    assert out.wall_s < 30


def test_supervisor_kills_a_worker_over_the_rss_guard(tmp_path: Path) -> None:
    out = _supervise(
        "b=bytearray(300*1024*1024);import time;time.sleep(60)", tmp_path, rss_kill_mb=150
    )
    failure = sup.classify_failure(out)
    assert out.rss_killed and failure is not None and failure["kind"] == "rss_ceiling_kill"


# ----------------------------------------------------------------------- full worker


def _worker(data_dir: Path, work_root: Path, *extra: str) -> tuple[int, dict]:
    proc = subprocess.run(  # noqa: S603 -- fixed in-repo script, test-controlled args
        [sys.executable, str(_SCRIPTS / "bench_worker_fk.py"), "--data-dir", str(data_dir),
         "--work-root", str(work_root), *extra],
        capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH_JSON "))
    return proc.returncode, json.loads(line.removeprefix("BENCH_JSON "))


@pytest.fixture(scope="module")
def small_chain(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("small")
    build_fk_chain.build(out, _COUNTS, width=2, orphan_frac=0.02)
    return out


def test_worker_emits_a_complete_record_at_small_n(small_chain: Path, tmp_path: Path) -> None:
    rc, rec = _worker(
        small_chain, tmp_path, "--budget-mb", "512", "--reorder", "100", "--rss-ceiling-mb", "4096"
    )
    assert rc == 0 and rec["schema"] == "fk-scale/v1"
    assert rec["completed"] is True and rec["failure"] is None
    assert rec["rows"] == _COUNTS and rec["total_rows"] == sum(_COUNTS.values())
    # Route: AUTO by default, with the engine's own reason recorded.
    assert rec["requested"]["route_forced"] is False
    assert rec["route"]["execution_mode"] == "out_of_core" and rec["route"]["route_reason"]
    # Reorder witness names the driver per table.
    assert rec["reorder"]["driver_by_table"]["child"] == "reorder"
    assert rec["reorder"]["taken"] is True
    # Native disposition is per column, not an import check.
    disp = rec["disposition"]
    assert (
        disp["columns_total"] > 0
        and disp["native_columns"] + len(disp["non_native"]) == disp["columns_total"]
    )
    # External RSS, spill, parity, row counts.
    assert rec["peak_rss_mb"] > 0 and rec["rss_method"] == "wait4_ru_maxrss"
    assert rec["rss_within_ceiling"] is True
    assert "temp_disk_peak_mb" in rec["spill"]
    assert rec["rowcount_match"] and rec["parity"]["status"] == "ok"
    assert rec["sizing"]["budget_bytes"] == 512 * 1024 * 1024
    assert rec["mask_s_p50"] == rec["mask_s"] and rec["mask_s_p95"] == rec["mask_s"]


def test_worker_reorder_off_and_forced_route_are_recorded(
    small_chain: Path, tmp_path: Path
) -> None:
    rc, rec = _worker(
        small_chain, tmp_path, "--budget-mb", "512", "--reorder", "off", "--force-ooc"
    )
    assert rc == 0 and rec["completed"]
    assert rec["route"]["forced"] is True and rec["requested"]["execution_mode"] == "out_of_core"
    assert rec["reorder"]["taken"] is False
    assert rec["reorder"]["threshold_rows"] == max(_COUNTS.values()) + 1


def test_worker_forced_oom_is_not_a_silent_pass(small_chain: Path, tmp_path: Path) -> None:
    # A tight RLIMIT_DATA cap forces a resource failure on brk-based allocators; where
    # large allocations go through mmap (seen on some CI runners) the cap does not bite,
    # so a short timeout backstop guarantees the run still ends as a classified, non-silent
    # failure instead of hanging. Either path: completed is False with a recorded reason.
    rc, rec = _worker(
        small_chain, tmp_path, "--budget-mb", "512", "--mem-cap-mb", "64", "--timeout-s", "45"
    )
    assert rc != 0
    assert rec["completed"] is False and rec["failure"] is not None
    assert rec["requested"]["mem_cap_kind"] == "RLIMIT_DATA"
    assert rec["peak_rss_mb"] > 0  # external measurement survives the failure
    assert rec["rss_within_ceiling"] is None


def test_worker_timeout_still_emits_a_record(small_chain: Path, tmp_path: Path) -> None:
    rc, rec = _worker(small_chain, tmp_path, "--budget-mb", "512", "--timeout-s", "0.4")
    assert rc != 0 and rec["completed"] is False and rec["failure"]["kind"] == "timeout"


def test_worker_repeats_report_percentiles(small_chain: Path, tmp_path: Path) -> None:
    rc, rec = _worker(small_chain, tmp_path, "--budget-mb", "512", "--repeats", "2")
    assert rc == 0 and len(rec["mask_s_runs"]) == 2 and len(rec["runs"]) == 2
    assert min(rec["mask_s_runs"]) <= rec["mask_s_p50"] <= max(rec["mask_s_runs"])
