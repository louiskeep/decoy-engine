"""Every file the out-of-core route writes under its scratch root is 0600 and every directory
0700, whatever the umask: the F06 typed staging copy and the route's spill (key stages, relation
and dedup files, sorter runs, payload stores) share that root and all hold source values.

Each file's mode is captured just before the engine removes it (``unlink``) or removes the root
(``rmtree``), so a file that lives only inside one statement is still seen. DuckDB's own block
spill under ``duckdb/`` is DuckDB's, not the engine's, and is excluded (see the module this
tests). The configured kinds must all be observed, so a route change cannot pass vacuously.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.acceptance._f06_support import INT64, column, run, zero_pad

UMASKS = (0o000, 0o077, 0o777)
ROWS = 3000


@pytest.fixture(params=UMASKS, ids=lambda m: f"umask{m:04o}")
def umask_value(request: pytest.FixtureRequest) -> int:
    return int(request.param)


@contextmanager
def _umask(value: int) -> Iterator[int]:
    old = os.umask(value)
    try:
        yield value
    finally:
        os.umask(old)


@pytest.fixture
def umask(umask_value: int) -> Iterator[int]:
    # Applied before the test body: only for tests that create no input of their own.
    with _umask(umask_value):
        yield umask_value


class _Recorder:
    """Mode of every path under ``base`` at the moment the engine removes it."""

    def __init__(self, base: Path) -> None:
        self.base = str(base) + os.sep
        self.files: dict[str, int] = {}
        self.dirs: dict[str, int] = {}

    def _note(self, path: Any) -> None:
        p = os.fspath(path)
        if not p.startswith(self.base):
            return
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return
        rel = p[len(self.base) :]
        (self.dirs if stat.S_ISDIR(st.st_mode) else self.files)[rel] = stat.S_IMODE(st.st_mode)

    def tree(self, top: Any) -> None:
        self._note(top)
        for dirpath, dirnames, filenames in os.walk(top):
            for name in (*dirnames, *filenames):
                self._note(os.path.join(dirpath, name))


@pytest.fixture
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    base = tmp_path / "tmp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(base))
    rec = _Recorder(base)
    real_unlink, real_os_unlink, real_rmtree = Path.unlink, os.unlink, shutil.rmtree

    def path_unlink(self: Path, *a: Any, **k: Any) -> None:
        rec._note(self)
        real_unlink(self, *a, **k)

    def os_unlink(path: Any, *a: Any, **k: Any) -> None:
        if isinstance(path, (str, os.PathLike)):
            rec._note(path)
        real_os_unlink(path, *a, **k)

    def rmtree(path: Any, *a: Any, **k: Any) -> None:
        rec.tree(path)
        real_rmtree(path, *a, **k)

    monkeypatch.setattr(Path, "unlink", path_unlink)
    monkeypatch.setattr(os, "unlink", os_unlink)
    monkeypatch.setattr(os, "remove", os_unlink)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    return rec


def _sources(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    from decoy_engine.profile._readers import LazySource

    ids = [str(i) for i in range(ROWS)]
    data = {
        "parent": pa.table({"id": ids, "label": ["p"] * ROWS}),
        "child": pa.table({"id": ids, "pid": ids, "c": [f"{i:06d}" for i in range(ROWS)]}),
    }
    paths: dict[str, Any] = {}
    for name, table in data.items():
        path = tmp_path / f"{name}.parquet"
        pq.write_table(table, path)
        paths[name] = path
    descriptors = {
        name: {"type": "file", "format": "parquet", "path": str(path)}
        for name, path in paths.items()
    }
    return descriptors, {name: LazySource(path) for name, path in paths.items()}


def _config(tmp_path: Path, descriptors: dict[str, Any]) -> dict[str, Any]:
    key = column("id", "hash", namespace="ns", deterministic=True)
    fk = column("pid", "hash", namespace="ns", deterministic=True)
    declared = column(
        "c",
        logical_type="integer",
        parse_format=INT64,
        output_format=zero_pad(6),
        error_policy="null",
    )
    return {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": descriptors,
        "tables": [
            {"name": "parent", "columns": [key, column("label")]},
            {"name": "child", "columns": [column("id"), fk, declared]},
        ],
        "targets": {
            t: {"type": "file", "format": "parquet", "path": str(tmp_path / f"{t}.out.parquet")}
            for t in descriptors
        },
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "remap",
                "namespace": "ns",
            }
        ],
        "namespaces": {},
    }


def _engine_owned(rel: str) -> bool:
    # DuckDB's own temp blocks (`duckdb_temp_*`) inside a `duckdb/` dir are created by DuckDB.
    return not os.path.basename(rel).startswith("duckdb_temp")


# Kinds the declared relational run must leave a trace of; observed, not assumed.
_EXPECTED = (
    ".typed.parquet",
    "_key_relation.parquet",
    "_key_staged.parquet",
    "_key_winners.parquet",
)


def test_out_of_core_scratch_files_are_0600_and_dirs_0700(
    tmp_path: Path, umask_value: int, recorder: _Recorder
) -> None:
    descriptors, lazy = _sources(tmp_path)  # inputs, and the recorder's temp base: before the umask
    with _umask(umask_value) as umask:
        result = run(_config(tmp_path, descriptors), lazy, execution_mode="out_of_core")
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"

    files = {k: v for k, v in recorder.files.items() if _engine_owned(k)}
    for suffix in _EXPECTED:
        assert any(k.endswith(suffix) for k in files), (suffix, sorted(files))
    loose_files = {k: oct(v) for k, v in files.items() if v != 0o600}
    loose_dirs = {k: oct(v) for k, v in recorder.dirs.items() if v != 0o700}
    assert not loose_files, (oct(umask), loose_files)
    assert not loose_dirs, (oct(umask), loose_dirs)


# -- the spill writers this small run does not reach, each driven directly ---------------------


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _batch(n: int, start: int = 0) -> pa.RecordBatch:
    return pa.record_batch(
        {
            "__decoy_row_nr": pa.array(range(start, start + n), pa.int64()),
            "v": pa.array([f"SEED{i:06d}" for i in range(start, start + n)], pa.string()),
        }
    )


def test_payload_and_key_spills_are_0600_while_open(tmp_path: Path, umask: int) -> None:
    from decoy_engine.execution.out_of_core._payload_store import (
        RawParentKeySpill,
        SpillChildKeys,
        SpillPayloadStore,
    )

    batch = _batch(10)
    payload = SpillPayloadStore(tmp_path / "p" / "payload.arrow")
    payload.append(batch)
    raw = RawParentKeySpill(tmp_path / "r" / "raw.arrow", batch.schema)
    raw.append(batch)
    child = SpillChildKeys(tmp_path / "c" / "child.arrow", batch.schema)
    try:
        for path in ("p/payload.arrow", "r/raw.arrow", "c/child.arrow"):
            assert _mode(tmp_path / path) == 0o600, (oct(umask), path, oct(_mode(tmp_path / path)))
        for d in ("p", "r", "c"):
            assert _mode(tmp_path / d) == 0o700, (oct(umask), d, oct(_mode(tmp_path / d)))
    finally:
        payload.close()
        raw.finalize()
        child.finalize()


def test_masked_key_stage_is_0600_before_any_row(tmp_path: Path, umask: int) -> None:
    from decoy_engine.execution.out_of_core._stage import MaskedKeyStager

    batch = _batch(4)
    path = tmp_path / "staged" / "t0000" / "masked_keys.parquet"
    stager = MaskedKeyStager(path, columns=("v",), fixed_schema=batch.schema)
    try:
        assert _mode(path) == 0o600, (oct(umask), oct(_mode(path)))
        assert _mode(path.parent) == 0o700, (oct(umask), oct(_mode(path.parent)))
        stager.add(batch, batch)
    finally:
        stager.close()
    assert pq.read_table(path).num_rows == 4  # the writer still produced a readable file


def test_external_sort_runs_and_merges_are_0600(
    tmp_path: Path, umask: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.out_of_core._external_sort import BoundedExternalSorter

    seen: dict[str, int] = {}
    real_unlink = Path.unlink

    def unlink(self: Path, *a: Any, **k: Any) -> None:
        if self.exists():
            seen[self.name] = _mode(self)
        real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink)
    spill = tmp_path / "sort"
    sorter = BoundedExternalSorter(spill, run_bytes_cap=4096, merge_fan_in=2)
    try:
        for start in range(0, 2000, 100):
            sorter.write(_batch(100, start))
        sorter.finish()
        for p in spill.iterdir():
            seen[p.name] = _mode(p)
        dir_mode = _mode(spill)
    finally:
        sorter.close()
    assert any(n.startswith("run_") for n in seen) and any(n.startswith("mergep") for n in seen)
    assert {n: oct(m) for n, m in seen.items() if m != 0o600} == {}, oct(umask)
    assert dir_mode == 0o700, (oct(umask), oct(dir_mode))


def test_multi_parent_merge_files_are_0600(
    tmp_path: Path, umask_value: int, recorder: _Recorder
) -> None:
    from decoy_engine.execution.out_of_core._multi_parent import merge_parent_relations
    from decoy_engine.execution.out_of_core._relation import ParentKeyRelation

    base = Path(tempfile.gettempdir())
    members = []
    for i in range(2):
        path = base / f"m{i}.parquet"
        keys = [f"k{j}" for j in range(i, i + 5)]
        pq.write_table(
            pa.table({"__decoy_fk_join_key": keys, "__decoy_masked_key": [k + "x" for k in keys]}),
            path,
        )
        members.append(ParentKeyRelation(path=path))
    with _umask(umask_value) as umask:  # the member inputs above are written before it applies
        merged = merge_parent_relations(members, temp_dir=base / "group_0", memory_limit=None)
    recorder._note(merged.path)
    recorder.tree(base / "group_0")
    files = {k: v for k, v in recorder.files.items() if k.startswith("group_0")}
    files = {k: v for k, v in files.items() if _engine_owned(k)}
    for suffix in (
        "merged_key_relation.parquet",
        "merged_key_staged.parquet",
        "merged_key_winners.parquet",
    ):
        assert any(k.endswith(suffix) for k in files), (suffix, sorted(files))
    assert {k: oct(v) for k, v in files.items() if v != 0o600} == {}, oct(umask)
    dirs = {k: oct(v) for k, v in recorder.dirs.items() if k.startswith("group_0") and v != 0o700}
    assert dirs == {}, oct(umask)
