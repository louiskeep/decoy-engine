"""T-PLAIN (F25 S1b): scratch works on a host without project quotas or fscrypt.

Each job runs with an arena as its scratch parent and must give the output the pre-arena (F06)
routes gave, pinned as a digest of the decoded rows. Later slices move the routes onto the arena
and keep these digests.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import _runner as runner_mod
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.fileaccess import FileAccessReadError, ScratchArena, scratch_native
from decoy_engine.fileaccess.adapters import arrow
from decoy_engine.providers_v2 import get_default_registry
from tests._keyed_secret import THROWAWAY_KEY_PROVIDER
from tests.fileaccess.test_x4_scratch_and_names import PLAIN, _jobs

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows cells are S1w")
_REG = get_default_registry()
_QUOTA_OPTIONS = ("prjquota", "pquota", "prjjquota", "test_dummy_encryption")
# Rows the F06 routes produce for `_jobs(*PLAIN)` (identical on every lane: route choice never
# changes output), as `_digest` prints them; computed on the S1b start commit.
F06_ROWS = "45aa765b12098b3f6b458566d329d47bdffa5c4f78a5f8231a2da6dae29676d4"


@pytest.fixture(autouse=True)
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(root))
    return root


def _mount_options(path: str) -> list[str]:
    """The options of the mount holding `path`: the longest mount point that prefixes it."""
    best, options = "", ""
    with open("/proc/self/mounts", encoding="utf-8") as mounts:
        for line in mounts:
            fields = line.split()
            point = fields[1].replace("\\040", " ")
            if (path == point or path.startswith(point.rstrip("/") + "/")) and len(point) >= len(
                best
            ):
                best, options = point, fields[3]
    return options.split(",")


def _digest(tables: dict[str, pa.Table]) -> str:
    rows = {name: tables[name].to_pylist() for name in sorted(tables)}
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def _lazy(tmp_path: Path, sources: dict[str, pa.Table]) -> dict[str, LazySource]:
    src = tmp_path / "src"
    src.mkdir()
    for table, data in sources.items():
        pq.write_table(data, src / f"{table}.parquet")
    return {table: LazySource(src / f"{table}.parquet") for table in sources}


class _IdentityCast:
    """A declared-source cast that keeps every value: the declared lane with F06's rows."""

    def __init__(self, tables: set[str]) -> None:
        self._tables = tables

    def tables(self) -> set[str]:
        return self._tables

    def cast_batch(self, table: str, batch: Any, offset: int = 0, **_: Any) -> Any:
        if isinstance(batch, pa.RecordBatch):
            batch = pa.Table.from_batches([batch])
        return SimpleNamespace(table=batch)

    def format_batch(self, table: str, out: pa.Table, batch: Any) -> tuple[pa.Table, None]:
        return out, None

    def rendered_schema(self, table: str, schema: pa.Schema) -> pa.Schema:
        return schema

    def finish(self) -> tuple[dict[str, Any], None]:
        return {}, None


def _run(lane: str, tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    plan, sources, graph = _jobs(*PLAIN)
    common: dict[str, Any] = {
        "registry": _REG,
        "relationship_graph": graph,
        "key_provider": THROWAWAY_KEY_PROVIDER,
        "temp_dir": scratch,
        "batch_rows": 1,  # one row per batch: every pass spools to scratch
    }
    if lane == "batch":
        return _digest(run_fk_out_of_core(plan, sources, **common).outputs)
    if lane == "declared":
        cast = _IdentityCast(set(sources))
        result = run_fk_out_of_core(plan, _lazy(tmp_path, sources), cast_run=cast, **common)
        return _digest(result.outputs)
    reorder = {"calls": 0}
    real = runner_mod.stream_table

    def counted(*a: Any, **k: Any) -> Any:
        reorder["calls"] += 1
        return real(*a, **k)

    monkeypatch.setattr(runner_mod, "stream_table", counted)
    out = tmp_path / "out"
    out.mkdir()
    run_fk_out_of_core(
        plan,
        _lazy(tmp_path, sources),
        sink=ParquetTransactionalSink(out / "result"),
        budget_bytes=1 << 30,
        temp_disk_budget_bytes=200 << 30,
        out_of_core_reorder_threshold_rows=0,
        **common,
    )
    assert reorder["calls"] >= 1, "the reorder lane did not run"
    written = {p.stem: pq.read_table(p) for p in (out / "result").glob("*.parquet")}
    return _digest(written)


@pytest.mark.parametrize("cell", ["batch", "reorder_sink", "declared", "error_path"])
def test_jobs_succeed_without_quota_or_fscrypt(
    tmp_path: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch, cell: str
) -> None:
    with ScratchArena.open() as arena:
        options = _mount_options(arena.local_path())
        assert options and not set(options) & set(_QUOTA_OPTIONS), options  # a plain host
        if cell == "error_path":  # ambient: a non-resource library failure is still value-free
            schema = pa.schema([("a", pa.int64())])
            failures = []
            with pytest.raises(FileAccessReadError) as info:
                with arrow.parquet_writer(arena, "t.parquet", schema) as writer:
                    writer.write_table(pa.table({"a": ["123-45-6789"]}))
            failures.append(info.value)
            with pytest.raises(FileAccessReadError) as info:
                scratch_native(pq.read_table, arena.local_path("absent.parquet"))
            failures.append(info.value)
            assert [e.field_key for e in failures] == ["engine.scratch"] * 2
            assert all(e.__cause__ is None and e.__context__ is None for e in failures)
            return
        digest = _run(cell, tmp_path, Path(arena.local_path("job")), monkeypatch)
        assert digest == F06_ROWS, digest
    assert [p.name for p in temp_root.iterdir()] == []
