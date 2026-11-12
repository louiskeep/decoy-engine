"""Every engine row directory (spill, staging, snapshot spill) comes from the protected allocator.

Each site is watched with a spy around the allocator call, so keeping the old raw `mkdtemp` or
`mkdir` plus `chmod` makes the matching test fail. The rest of the behavior (modes, reopen checks,
removal) is the allocator's own and is covered in `tests/fileaccess/protected_dir`.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import fileaccess
from decoy_engine.connectors import postgres_snapshot as ps
from decoy_engine.execution import _isolated_run
from decoy_engine.execution.out_of_core import _runner, _scratch, run_fk_out_of_core
from decoy_engine.fileaccess import ProtectedDirUnsafe
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from tests.fileaccess.protected_dir.test_protected_dir import _Strict
from tests.unit.execution.test_out_of_core_join_chunked import _edge, _plan

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows lane is L0.1w")


def _spy(monkeypatch: pytest.MonkeyPatch, module: Any, name: str) -> list[tuple[Any, ...]]:
    """Record every call to `module.name` and pass it through to the real function."""
    real = getattr(module, name)
    calls: list[tuple[Any, ...]] = []

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return calls


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


# -- P0-ooc-routed -------------------------------------------------------------------------------


def test_ooc_private_dir_under_an_arena_root_under_any_policy(tmp_path: Path) -> None:
    jail = tmp_path / "jail"
    jail.mkdir()
    policy = _Strict(jail)
    try:
        with fileaccess.ScratchArena.open() as arena, fileaccess.scope(policy):
            child = _scratch.private_dir(Path(arena.local_path()) / "a" / "b")
            assert child.is_dir() and _mode(child) == 0o700
    finally:
        policy.close()


def test_ooc_private_dir_uses_open_protected_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _spy(monkeypatch, _scratch, "open_protected_dir")
    _scratch.private_dir(tmp_path / "x" / "z")
    assert [c[1] for c in calls] == [{"create": True, "parents": True}]
    assert _mode(tmp_path / "x") == _mode(tmp_path / "x" / "z") == 0o700


def test_ooc_the_scratch_module_makes_no_raw_directory() -> None:
    source = Path(_scratch.__file__).read_text(encoding="utf-8")
    assert "mkdtemp" not in source and "os.mkdir" not in source


# -- P0-isolated-routed --------------------------------------------------------------------------


def _run_isolated(staging_dir: Path | None, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Drive `_run_isolated` up to the spawn, returning the work roots the spawn saw."""
    seen: list[Path] = []

    def fake_spawn(payload_path: Path, *_: Any, **__: Any) -> str:
        seen.append(payload_path.parent)
        assert payload_path.parent.is_dir() and _mode(payload_path.parent) == 0o700
        return "spawned"

    monkeypatch.setattr(_isolated_run, "_spawn_and_classify", fake_spawn)
    result = _isolated_run._run_isolated(
        {},
        None,
        mem_cap_bytes=None,
        rlimit_kind="data",
        staging_dir=staging_dir,
        output_dir=None,
        on_spawn=None,
        timeout_s=None,
        run_pipeline_kwargs={},
    )
    assert result == "spawned"
    return seen


def test_isolated_default_work_root_comes_from_the_allocator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy(monkeypatch, _isolated_run.fileaccess, "protected_scratch_dir")
    (root,) = _run_isolated(None, monkeypatch)
    assert len(calls) == 1 and calls[0][1] == {"prefix": "decoy-isolated-run-"}
    assert not root.exists()  # removed when the run returns


def test_isolated_caller_named_staging_dir_comes_from_the_allocator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _spy(monkeypatch, _isolated_run.fileaccess, "open_protected_dir")
    staging = tmp_path / "work_root"
    old = os.umask(0)
    try:
        (root,) = _run_isolated(staging, monkeypatch)
    finally:
        os.umask(old)
    assert root == staging and [c[1] for c in calls] == [{"create": True}]
    assert _mode(staging) == 0o700
    assert staging.is_dir()  # a caller-named directory is left in place


def test_isolated_symlinked_staging_dir_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "real").mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(ProtectedDirUnsafe):
        _run_isolated(tmp_path / "link", monkeypatch)
    assert list((tmp_path / "real").iterdir()) == []


def test_isolated_work_root_is_removed_when_the_spawn_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots: list[Path] = []

    def boom(payload_path: Path, *_: Any, **__: Any) -> None:
        roots.append(payload_path.parent)
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(_isolated_run, "_spawn_and_classify", boom)
    with pytest.raises(RuntimeError):
        _isolated_run._run_isolated(
            {},
            None,
            mem_cap_bytes=None,
            rlimit_kind="data",
            staging_dir=None,
            output_dir=None,
            on_spawn=None,
            timeout_s=None,
            run_pipeline_kwargs={},
        )
    assert roots and not roots[0].exists()


# -- P0-pg-spill-routed --------------------------------------------------------------------------


def test_pg_spill_dir_comes_from_open_protected_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("duckdb")
    calls = _spy(monkeypatch, ps, "open_protected_dir")
    spill = tmp_path / "a" / "spill-1"
    config = ps.PostgresSnapshotConfig(
        dsn="postgresql://u:p@localhost/db",  # type: ignore[arg-type]
        extension_directory=tmp_path / "ext",
        memory_limit="64MB",
        temp_directory=tmp_path / "a",
        threads=1,
        row_group_size=1_000,
        statement_timeout_s=1,
        deadline_s=1,
    )
    old = os.umask(0)
    try:
        try:
            conn = ps._open_connection(config, spill)
        except ps.PostgresSnapshotError:
            conn = None  # connecting may fail offline; the directory was made first
    finally:
        os.umask(old)
    if conn is not None:
        conn.close()
    assert [c[1] for c in calls] == [{"create": True, "parents": True}]
    assert _mode(tmp_path / "a") == _mode(spill) == 0o700


def test_pg_symlinked_spill_dir_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir(mode=0o700)
    (tmp_path / "spill").symlink_to(tmp_path / "real")
    config = ps.PostgresSnapshotConfig(
        dsn="postgresql://u:p@localhost/db",  # type: ignore[arg-type]
        extension_directory=tmp_path / "ext",
        memory_limit="64MB",
        temp_directory=tmp_path,
        threads=1,
        row_group_size=1_000,
        statement_timeout_s=1,
        deadline_s=1,
    )
    with pytest.raises(ProtectedDirUnsafe):
        ps._open_connection(config, tmp_path / "spill")


# -- the runner removes what it made -------------------------------------------------------------


def _small_job() -> tuple[Any, Any, dict[str, pa.Table]]:
    graph = RelationshipGraph(edges=(_edge(),), ordering=())
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table({"customer_id": ["c1", "c2", "c1"], "amount": [1, 2, 3]}),
    }
    return _plan(), graph, sources


def _run_small(**kwargs: Any) -> None:
    plan, graph, sources = _small_job()
    run_fk_out_of_core(
        plan, sources, registry=get_default_registry(), relationship_graph=graph, **kwargs
    )


def _ooc_dirs(base: Path) -> list[str]:
    return [p.name for p in base.iterdir() if p.name.startswith("decoy-arena-")]


@pytest.mark.parametrize("fail", [False, True])
def test_runner_removes_its_engine_made_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: bool
) -> None:
    sys_tmp = tmp_path / "systmp"
    sys_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(sys_tmp))
    roots = _spy(monkeypatch, fileaccess.ScratchArena, "open")
    if fail:

        def boom(*_: Any, **__: Any) -> None:
            assert _ooc_dirs(sys_tmp)  # the root exists while the run is live
            raise RuntimeError("route failed")

        monkeypatch.setattr(_runner, "decide_route", boom)
        with pytest.raises(RuntimeError):
            _run_small()
    else:
        _run_small()
    assert [c[1] for c in roots] == [{"under": None}]
    assert _ooc_dirs(sys_tmp) == []


@pytest.mark.parametrize("fail", [False, True])
def test_runner_wipes_its_subtrees_but_keeps_a_caller_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: bool
) -> None:
    work = tmp_path / "work"
    if fail:

        def boom(*_: Any, **__: Any) -> None:
            raise RuntimeError("route failed")

        monkeypatch.setattr(_runner, "decide_route", boom)
        with pytest.raises(RuntimeError):
            _run_small(temp_dir=work)
    else:
        _run_small(temp_dir=work)
    assert work.is_dir() and _mode(work) == 0o700
    for sub in ("relations", "joins", "staged"):
        assert not (work / sub).exists()
