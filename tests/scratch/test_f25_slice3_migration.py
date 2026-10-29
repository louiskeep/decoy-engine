"""F25 Slice 3 acceptance (A01-A04 / A12 witnesses) for T09-T17.

Each migrated producer is driven once through a real ``ScratchManager`` attempt
arena (the Slice 1 subsystem, exercised with the dev-box tmpfs provider double)
and once on the pre-activation path, proving:

* the temporary bytes are allocated through the capability seam and registered
  in the attempt ledger (A01/A04 coverage), and
* the pre-activation output is byte-identical to the migrated one where the
  producer carries a deterministic output-byte contract (A04 parity).

The EDQUOT -> shared-report wiring (A02 behavior) is unit-tested by INJECTING a
quota ``OSError`` at a write site on the dev box; the real-kernel-quota A02/A03
filesystem re-run is Slice 4 on the GCP host, not here.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import decoy_engine
from decoy_engine.scratch import Arena, ScratchManager, ScratchQuotaExceeded
from tests.scratch._fakes import FakeAuthority, QualifiedTmpfsProvider, make_policy


def _open_arena(tmp_path: Path) -> tuple[ScratchManager, Arena]:
    stage_root = tmp_path / "stage"
    stage_root.mkdir()
    provider = QualifiedTmpfsProvider()
    manager = ScratchManager.open_pool(
        pool_id="slice3", control_arena=tmp_path / "control", provider=provider
    )
    arena = manager.open_attempt(make_policy(stage_root), provider, FakeAuthority())
    return manager, arena


def _ledger_kinds(arena: Arena) -> set[str]:
    rows = arena._ledger.list_resources(arena.arena_id)
    return {getattr(r.kind, "value", str(r.kind)) for r in rows}


def _has_kind(arena: Arena, needle: str) -> bool:
    return needle in _ledger_kinds(arena)


# --------------------------------------------------------------------------
# T09 transactional sink
# --------------------------------------------------------------------------


def test_t09_transactional_sink_stages_under_injected_arena(tmp_path: Path) -> None:
    manager, arena = _open_arena(tmp_path)
    try:
        out = tmp_path / "out"
        sink = ParquetTransactionalSink(out, scratch_arena=arena)
        table = pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        sink.write("customers", table)
        assert sink._staging is not None
        # A publication stage (.decoy-stage-*), not the _decoy_stage_ fallback.
        assert sink._staging.name.startswith(".decoy-stage-")
        assert _has_kind(arena, "publication")
        sink.commit()
        assert pq.read_table(out / "customers.parquet").to_pydict() == table.to_pydict()
    finally:
        manager.close()


def test_t09_a04_parity_with_pre_activation_path(tmp_path: Path) -> None:
    manager, arena = _open_arena(tmp_path)
    try:
        table = pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        arena_out = tmp_path / "arena_out"
        plain_out = tmp_path / "plain_out"
        for out, kw in ((arena_out, {"scratch_arena": arena}), (plain_out, {})):
            sink = ParquetTransactionalSink(out, **kw)
            sink.write("customers", table)
            sink.commit()
        a = (arena_out / "customers.parquet").read_bytes()
        p = (plain_out / "customers.parquet").read_bytes()
        assert a == p, "migrated output is not byte-identical to the pre-activation path"
    finally:
        manager.close()


def test_t09_edquot_on_write_routes_to_shared_report(tmp_path: Path, monkeypatch) -> None:
    manager, arena = _open_arena(tmp_path)
    try:
        sink = ParquetTransactionalSink(tmp_path / "o", scratch_arena=arena)

        def _boom(*_a, **_k):
            raise OSError(errno.EDQUOT, "disk quota exceeded")

        monkeypatch.setattr(pq, "write_table", _boom)
        with pytest.raises(ScratchQuotaExceeded) as excinfo:
            sink.write("customers", pa.table({"a": [1]}))
        report = excinfo.value.report
        assert report.errno == errno.EDQUOT
        assert report.disposition.value == "retained"
        assert report.arena_id == arena.arena_id
    finally:
        manager.close()


def test_t09_non_quota_oserror_passes_through(tmp_path: Path, monkeypatch) -> None:
    manager, arena = _open_arena(tmp_path)
    try:
        sink = ParquetTransactionalSink(tmp_path / "o", scratch_arena=arena)

        def _boom(*_a, **_k):
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(pq, "write_table", _boom)
        with pytest.raises(OSError) as excinfo:
            sink.write("customers", pa.table({"a": [1]}))
        assert not isinstance(excinfo.value, ScratchQuotaExceeded)
        assert excinfo.value.errno == errno.EACCES
    finally:
        manager.close()


# --------------------------------------------------------------------------
# T10 chunked output hold-back spill
# --------------------------------------------------------------------------


def test_t10_chunked_hold_back_spills_under_injected_arena(tmp_path: Path) -> None:
    from decoy_engine.execution._chunked_output_sink import StreamStats, _Spill

    manager, arena = _open_arena(tmp_path)
    try:
        spill = _Spill(StreamStats(), sink=object(), table="t", arena=arena)
        chunk = pa.table({"a": [1, 2, 3]})
        spill.add(chunk)
        assert spill.directory is not None
        root = Path(arena.capability.approved_root).resolve()
        assert Path(spill.directory).resolve().is_relative_to(root)
        assert [t.to_pydict() for t in spill.replay()] == [chunk.to_pydict()]
        spill.close()
    finally:
        manager.close()


def test_t10_missing_capability_failure_preserved() -> None:
    from decoy_engine.execution._chunked_output_sink import StreamStats, _Spill
    from decoy_engine.execution._errors import ExecutionError

    # No arena and a sink with no spill_parent: the hold-back must fail closed.
    spill = _Spill(StreamStats(), sink=object(), table="t", arena=None)
    with pytest.raises(ExecutionError) as excinfo:
        spill.add(pa.table({"a": [1]}))
    assert excinfo.value.code == "hold_back_spill_unavailable"


# --------------------------------------------------------------------------
# T11 isolated run: capability transport + work-root registration
# --------------------------------------------------------------------------


def test_t11_capability_descriptor_and_fd_roundtrip(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_cap import (
        CAP_FD_ENV,
        capability_descriptor,
        open_capability_channel,
        read_inherited_capability,
    )

    manager, arena = _open_arena(tmp_path)
    try:
        descriptor = capability_descriptor(arena)
        assert descriptor is not None
        assert descriptor["arena_id"] == arena.arena_id
        assert descriptor["approved_root"] == str(arena.capability.approved_root)

        read_fd, write_fd = open_capability_channel(descriptor)
        os.close(write_fd)
        got = read_inherited_capability({CAP_FD_ENV: str(read_fd)})
        assert got == descriptor
        # Cleared after the single read: the fd is closed.
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        manager.close()


def test_t11_pre_activation_transports_no_capability(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_cap import capability_descriptor
    from decoy_engine.scratch import LocalArena

    assert capability_descriptor(None) is None
    assert capability_descriptor(LocalArena(tmp_path / "s")) is None


def test_t11_isolated_run_registers_work_root_under_arena(tmp_path: Path) -> None:
    from decoy_engine.execution import run_pipeline_isolated

    manager, arena = _open_arena(tmp_path)
    try:
        cfg = _mask_config(tmp_path)
        sources = _mask_sources(tmp_path, n_rows=5)
        out = tmp_path / "iso_out"
        # The child subprocess must load THIS worktree's engine (the editable
        # venv points at the main checkout), so put the worktree src first.
        src_dir = Path(decoy_engine.__file__).resolve().parents[1]
        existing = os.environ.get("PYTHONPATH", "")
        env_path = str(src_dir) + (os.pathsep + existing if existing else "")
        os.environ["PYTHONPATH"] = env_path
        try:
            result = run_pipeline_isolated(
                cfg, sources, engine_version="f25", output_dir=str(out), scratch_arena=arena
            )
        finally:
            if existing:
                os.environ["PYTHONPATH"] = existing
            else:
                os.environ.pop("PYTHONPATH", None)
        assert result.outcome == "completed", result.error
        assert out.exists()
        # The parent registered the work root (native_dir) and the sibling output
        # stage (publication) in the attempt ledger.
        assert _has_kind(arena, "native_dir")
        assert _has_kind(arena, "publication")
    finally:
        manager.close()


# --------------------------------------------------------------------------
# T12 isolated commit (in-process fallback staging)
# --------------------------------------------------------------------------


def test_t12_isolated_commit_stages_under_injected_arena(tmp_path: Path) -> None:
    from decoy_engine.execution import run_pipeline_isolated

    manager, arena = _open_arena(tmp_path)
    try:
        cfg = _mask_config(tmp_path)
        sources = _mask_sources(tmp_path, n_rows=8)
        out = tmp_path / "fallback_out"
        result = run_pipeline_isolated(
            cfg,
            sources,
            engine_version="f25",
            output_dir=str(out),
            isolate=False,
            scratch_arena=arena,
        )
        assert result.outcome == "completed", result.error
        assert out.exists()
        assert _has_kind(arena, "publication")
    finally:
        manager.close()


# --------------------------------------------------------------------------
# T13 postgres snapshot: spill/stage allocated through the seam before connect
# --------------------------------------------------------------------------


def test_t13_postgres_snapshot_dirs_resolve_through_seam(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from decoy_engine.connectors.postgres_snapshot import (
        PostgresSnapshotConfig,
        PostgresSnapshotError,
        RelationSelection,
        snapshot_tables_to_parquet,
    )

    manager, arena = _open_arena(tmp_path)
    try:
        ext_dir = tmp_path / "ext"
        ext_dir.mkdir()
        config = PostgresSnapshotConfig(
            dsn=SecretStr("postgresql://invalid:invalid@127.0.0.1:1/none"),
            extension_directory=ext_dir,
            memory_limit="256MB",
            temp_directory=tmp_path / "pg_tmp",
            threads=1,
            row_group_size=100_000,
            statement_timeout_s=1,
            deadline_s=1,
        )
        selection = RelationSelection(schema="public", table="t", columns=["id"])
        dest = tmp_path / "snap_dest"
        # The connection cannot succeed (no extension / unreachable source); the
        # spill and stage are allocated from the arena BEFORE connect, so the
        # ledger records them regardless of the failure.
        with pytest.raises(PostgresSnapshotError):
            snapshot_tables_to_parquet(config, [selection], dest, scratch_arena=arena)
        assert _has_kind(arena, "native_dir")
        assert _has_kind(arena, "publication")
    finally:
        manager.close()


# --------------------------------------------------------------------------
# T14 subset: stage-then-transfer publication
# --------------------------------------------------------------------------


def test_t14_subset_publishes_via_staged_transfer(tmp_path: Path) -> None:
    from decoy_engine.subset._materialize import materialize_subset

    manager, arena = _open_arena(tmp_path)
    try:
        src = tmp_path / "src.parquet"
        pq.write_table(pa.table({"id": [1, 2, 3, 4], "v": ["a", "b", "c", "d"]}), src)
        from decoy_engine.subset._types import SubsetSource

        sources = {"t": SubsetSource(path=str(src), format="parquet")}
        survivors = {"t": frozenset({0, 2})}

        arena_dir = tmp_path / "arena_sub"
        materialize_subset(
            sources=sources, survivors=survivors, output_dir=arena_dir, scratch_arena=None
        )
        # Same inputs through a stage: write into a registered stage then rename.
        stage = tmp_path / ".stage_probe"
        materialize_subset(
            sources=sources, survivors=survivors, output_dir=stage, scratch_arena=arena
        )
        a = pq.read_table(arena_dir / "t.parquet").to_pydict()
        s = pq.read_table(stage / "t.parquet").to_pydict()
        assert a == s == {"id": [1, 3], "v": ["a", "c"]}
    finally:
        manager.close()


# --------------------------------------------------------------------------
# T15 quarantine staged JSONL
# --------------------------------------------------------------------------


def test_t15_quarantine_stage_under_injected_arena(tmp_path: Path) -> None:
    from decoy_engine.quarantine import finalize_committed_quarantine

    manager, arena = _open_arena(tmp_path)
    try:
        out = tmp_path / "out"
        out.mkdir()

        class _Sink:
            def committed_table_path(self, table: str) -> Path:
                return out / f"{table}.parquet"

        final = out / "quarantine.jsonl"
        entries = [{"table": "t", "row_index": 1, "trigger": "x", "reason": "r"}]
        result = finalize_committed_quarantine(
            sink=_Sink(),
            is_genuine_transactional_sink=True,
            written_tables=["t"],
            output_path=str(final),
            entries=entries,
            counts_by_trigger={"x": 1},
            scratch_arena=arena,
        )
        assert result is not None
        assert final.exists()
        assert _has_kind(arena, "publication")
    finally:
        manager.close()


def test_t15_direct_durable_jsonl_is_unchanged(tmp_path: Path) -> None:
    from decoy_engine.quarantine import finalize_committed_quarantine

    # No genuine transactional sink: the direct durable JSONL path writes
    # straight to output_path, not a staged file (sanctioned durable sink).
    final = tmp_path / "quarantine.jsonl"
    finalize_committed_quarantine(
        sink=None,
        is_genuine_transactional_sink=False,
        written_tables=[],
        output_path=str(final),
        entries=[{"table": "t", "row_index": 0, "trigger": "x", "reason": "r"}],
        counts_by_trigger={"x": 1},
    )
    assert final.exists()


# --------------------------------------------------------------------------
# T16 fixed-width writer
# --------------------------------------------------------------------------


def _fw_layout() -> dict:
    return {
        "columns": [
            {"name": "a", "start": 0, "width": 3, "type": "str", "pad": " ", "align": "left"},
            {"name": "b", "start": 3, "width": 3, "type": "int", "pad": "0", "align": "right"},
        ]
    }


def test_t16_fixed_width_writes_under_injected_arena(tmp_path: Path) -> None:
    from decoy_engine.profile._fixed_width_writer import write_fixed_width

    manager, arena = _open_arena(tmp_path)
    try:
        frame = pd.DataFrame({"a": ["x", "yy"], "b": [1, 42]})
        arena_path = tmp_path / "arena.fw"
        plain_path = tmp_path / "plain.fw"
        write_fixed_width(frame, _fw_layout(), str(arena_path), scratch_arena=arena)
        write_fixed_width(frame, _fw_layout(), str(plain_path))
        assert arena_path.read_bytes() == plain_path.read_bytes()
        assert _has_kind(arena, "publication")
    finally:
        manager.close()


def test_t16_edquot_on_fsync_routes_to_shared_report(tmp_path: Path, monkeypatch) -> None:
    from decoy_engine.profile import _fixed_width_writer

    manager, arena = _open_arena(tmp_path)
    try:
        frame = pd.DataFrame({"a": ["x"], "b": [1]})

        def _boom(_fd):
            raise OSError(errno.EDQUOT, "disk quota exceeded")

        monkeypatch.setattr(_fixed_width_writer.os, "fsync", _boom)
        with pytest.raises(ScratchQuotaExceeded) as excinfo:
            _fixed_width_writer.write_fixed_width(
                frame, _fw_layout(), str(tmp_path / "x.fw"), scratch_arena=arena
            )
        assert excinfo.value.report.errno == errno.EDQUOT
    finally:
        manager.close()


# --------------------------------------------------------------------------
# T17 discovery DuckDB spill
# --------------------------------------------------------------------------


def test_t17_discovery_spill_configured_from_arena(tmp_path: Path) -> None:
    from decoy_engine.data_discovery import run_discovery_sql

    manager, arena = _open_arena(tmp_path)
    try:
        src = tmp_path / "t.parquet"
        pq.write_table(pa.table({"a": [1, 2, 3]}), src)
        result = run_discovery_sql(
            "SELECT count(*) AS n FROM t", {"t": str(src)}, scratch_arena=arena
        )
        assert result.rows == [{"n": 3}]
        assert _has_kind(arena, "native_dir")
    finally:
        manager.close()


def test_t17_discovery_without_arena_still_runs(tmp_path: Path) -> None:
    from decoy_engine.data_discovery import run_discovery_sql

    src = tmp_path / "t.parquet"
    pq.write_table(pa.table({"a": [1, 2, 3]}), src)
    result = run_discovery_sql("SELECT a FROM t ORDER BY a", {"t": str(src)})
    assert result.rows == [{"a": 1}, {"a": 2}, {"a": 3}]


# --------------------------------------------------------------------------
# OOC (T01-T08) EDQUOT -> out_of_core_temp_disk_exceeded with the shared report
# --------------------------------------------------------------------------


def test_ooc_edquot_surfaces_as_out_of_core_temp_disk_exceeded_with_report(
    tmp_path: Path, monkeypatch
) -> None:
    """A kernel quota trip in an out-of-core writer is translated at the runner
    choke point into the public out_of_core_temp_disk_exceeded error with the
    shared scratch_quota report attached (plan 1.5 item 5)."""
    from decoy_engine.execution._errors import ExecutionError
    from decoy_engine.execution.out_of_core import _stage
    from decoy_engine.execution.out_of_core._runner import run_fk_out_of_core
    from tests.physical._helpers import build_job

    manager, arena = _open_arena(tmp_path)
    try:
        fix = tmp_path / "fix"
        fix.mkdir()
        specs = [
            {
                "name": "parent",
                "columns": [{"name": "id", "strategy": "hash", "namespace": "pns"}],
            },
            {
                "name": "child",
                "columns": [{"name": "pid", "strategy": "hash", "namespace": "pns"}],
            },
        ]
        rels = [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": "pns",
            }
        ]
        srcs = {
            "parent": pa.table({"id": pa.array([f"p{i}" for i in range(20)], pa.string())}),
            "child": pa.table({"pid": pa.array([f"p{i % 20}" for i in range(20)], pa.string())}),
        }
        job = build_job(fix, srcs, specs, relationships=rels)

        def _boom(self, *_a, **_k):
            raise OSError(errno.EDQUOT, "disk quota exceeded")

        monkeypatch.setattr(_stage.MaskedKeyStager, "add", _boom)
        with pytest.raises(ExecutionError) as excinfo:
            run_fk_out_of_core(
                job.plan,
                job.sources,
                registry=job.registry,
                relationship_graph=job.graph,
                sink=ParquetTransactionalSink(tmp_path / "ooc_out"),
                temp_dir=tmp_path / "ooc_work",
                scratch_arena=arena,
            )
        assert excinfo.value.code == "out_of_core_temp_disk_exceeded"
        report = getattr(excinfo.value, "report", None)
        assert report is not None, "the shared quota report was not attached"
        assert report.errno == errno.EDQUOT
        assert report.arena_id == arena.arena_id
    finally:
        manager.close()


# --------------------------------------------------------------------------
# Shared fixtures (a tiny pure-mask job).
# --------------------------------------------------------------------------

from decoy_engine.config import PipelineConfig  # noqa: E402
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink  # noqa: E402


def _mask_config(tmp_path: Path) -> dict:
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "customers.csv"),
                }
            },
            "tables": [{"name": "customers", "columns": [{"name": "col0", "strategy": "redact"}]}],
            "targets": {
                "customers": {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}
            },
        }
    ).model_dump()


def _mask_sources(tmp_path: Path, n_rows: int) -> dict[str, pa.Table]:
    df = pd.DataFrame({"col0": [f"value-{r}" for r in range(n_rows)]})
    df.to_csv(tmp_path / "customers.csv", index=False)
    return {"customers": pa.Table.from_pandas(df, preserve_index=False)}
