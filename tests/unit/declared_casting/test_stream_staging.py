"""The typed staging copy of an out-of-core declared source honours the caller's temp root and
counts against the temp-disk budget, like every other spill of the run."""

from __future__ import annotations

import errno
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROWS = 20_000


class _Run:
    def tables(self) -> list[str]:
        return ["t"]

    def cast_batch(self, table: str, batch: pa.Table, offset: int) -> Any:
        return SimpleNamespace(table=pa.Table.from_batches([batch]))


def _lazy(tmp_path: Path) -> Any:
    from decoy_engine.profile._readers import LazySource

    path = tmp_path / "src.parquet"
    # incompressible payload so the staged copy has a predictable, non-trivial size
    pq.write_table(pa.table({"v": pa.array([str(i * 7919) for i in range(ROWS)])}), path)
    return LazySource(path)


def _call(tmp_path: Path, seen: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"cast_run": _Run(), "plan": object(), "sources": {"t": _lazy(tmp_path)}, **extra}


def _fn(seen: dict[str, Any]) -> Any:
    def fn(plan: Any, sources: Any, **rest: Any) -> str:
        path = Path(sources["t"].path)
        seen["path"] = path
        seen["existed"] = path.exists()
        return "ok"

    return fn


def test_staged_copy_lives_under_the_callers_temp_dir_and_is_removed(tmp_path: Path) -> None:
    from decoy_engine.declared_casting._stream import run_staged

    root = tmp_path / "caller-root"
    seen: dict[str, Any] = {}
    out = run_staged(_fn(seen), _call(tmp_path, seen, temp_dir=root), 1000)
    assert out == "ok" and seen["existed"]
    assert root in seen["path"].parents
    assert root.is_dir() and not any(root.iterdir()), "only the staging dir is removed"


def test_staged_copy_counts_against_the_temp_disk_budget(tmp_path: Path) -> None:
    from decoy_engine.declared_casting._stream import run_staged
    from decoy_engine.execution._errors import ExecutionError

    root = tmp_path / "caller-root"
    seen: dict[str, Any] = {}
    with pytest.raises(ExecutionError) as info:
        run_staged(
            _fn(seen), _call(tmp_path, seen, temp_dir=root, temp_disk_budget_bytes=1024), 1000
        )
    assert str(info.value.code) == "out_of_core_temp_disk_exceeded"
    assert "path" not in seen, "the route never ran over an over-budget copy"
    assert not any(root.iterdir()), "an over-budget copy is not left behind"


def test_budget_applies_without_a_caller_temp_dir(tmp_path: Path) -> None:
    from decoy_engine.declared_casting._stream import run_staged
    from decoy_engine.execution._errors import ExecutionError

    with pytest.raises(ExecutionError) as info:
        run_staged(_fn({}), _call(tmp_path, {}, temp_disk_budget_bytes=1024), 1000)
    assert str(info.value.code) == "out_of_core_temp_disk_exceeded"


def _spilling_fn(seen: dict[str, Any], spill_bytes: int) -> Any:
    """Disk handling of `run_fk_out_of_core`: its own root when no ``temp_dir`` arrives, spill
    under the root, the budget checked over the root at a table boundary."""
    import os
    import shutil
    import tempfile

    from decoy_engine.execution.out_of_core._budget import check_temp_disk_budget

    def fn(
        plan: Any,
        sources: Any,
        *,
        temp_dir: Path | None = None,
        temp_disk_budget_bytes: int | None = None,
        **rest: Any,
    ) -> str:
        owned = temp_dir is None
        root = Path(tempfile.mkdtemp(prefix="decoy-ooc-")) if temp_dir is None else Path(temp_dir)
        seen["root"], seen["staged"] = root, Path(sources["t"].path)
        try:
            (root / "joins").mkdir(parents=True, exist_ok=True)
            (root / "joins" / "spill.bin").write_bytes(os.urandom(spill_bytes))
            assert temp_disk_budget_bytes is not None
            check_temp_disk_budget(root, max_bytes=temp_disk_budget_bytes)
        finally:
            shutil.rmtree(root / "joins", ignore_errors=True)
            if owned:
                shutil.rmtree(root, ignore_errors=True)
        return "ok"

    return fn


def _staged_bytes(tmp_path: Path) -> int:
    from decoy_engine.declared_casting._stream import run_staged

    tmp_path.mkdir()
    seen: dict[str, Any] = {}

    def measure(plan: Any, sources: Any, **rest: Any) -> str:
        seen["bytes"] = Path(sources["t"].path).stat().st_size
        return "ok"

    run_staged(measure, _call(tmp_path, seen), 1000)
    return int(seen["bytes"])


@pytest.mark.parametrize("caller_temp_dir", [True, False])
def test_staged_copy_and_spill_share_one_budget(tmp_path: Path, caller_temp_dir: bool) -> None:
    from decoy_engine.declared_casting._stream import run_staged
    from decoy_engine.execution._errors import ExecutionError

    staged = _staged_bytes(tmp_path / "m")
    spill = staged
    budget = staged + spill // 2  # each alone fits, together they do not
    extra = {"temp_dir": tmp_path / "caller-root"} if caller_temp_dir else {}
    seen: dict[str, Any] = {}
    with pytest.raises(ExecutionError) as info:
        run_staged(
            _spilling_fn(seen, spill),
            _call(tmp_path, seen, temp_disk_budget_bytes=budget, **extra),
            1000,
        )
    assert str(info.value.code) == "out_of_core_temp_disk_exceeded"
    assert seen["root"] in seen["staged"].parents, "the spill root holds the staged copy"
    assert not seen["root"].exists(), "the per-run root is removed on failure"


@pytest.mark.parametrize("caller_temp_dir", [True, False])
def test_staged_copy_and_spill_within_one_budget_run(tmp_path: Path, caller_temp_dir: bool) -> None:
    from decoy_engine.declared_casting._stream import run_staged

    staged = _staged_bytes(tmp_path / "m")
    caller = tmp_path / "caller-root"
    extra = {"temp_dir": caller} if caller_temp_dir else {}
    seen: dict[str, Any] = {}
    out = run_staged(
        _spilling_fn(seen, staged // 2),
        _call(tmp_path, seen, temp_disk_budget_bytes=4 * staged, **extra),
        1000,
    )
    assert out == "ok"
    assert seen["root"] in seen["staged"].parents
    assert not seen["root"].exists()
    if caller_temp_dir:
        assert caller in seen["root"].parents and not any(caller.iterdir())


class _NamedRun(_Run):
    def __init__(self, name: str) -> None:
        self.name = name

    def tables(self) -> list[str]:
        return [self.name]


@pytest.mark.parametrize("hostile", ["../x", "absolute"])
def test_a_table_name_never_places_the_staged_copy(tmp_path: Path, hostile: str) -> None:
    """The staged file is named by the engine, so a table name with `..` or an absolute path
    cannot put raw source rows outside the private per-run root."""
    from decoy_engine.declared_casting._stream import run_staged

    caller = tmp_path / "caller-root"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    name = str(elsewhere / "x") if hostile == "absolute" else "../x"
    seen: dict[str, Any] = {}

    def fn(plan: Any, sources: Any, *, temp_dir: Path, **rest: Any) -> str:
        seen["root"] = Path(temp_dir).resolve()
        seen["staged"] = Path(sources[name].path).resolve()
        seen["outside"] = sorted(str(p) for p in tmp_path.rglob("*.parquet"))
        return "ok"

    call = {"cast_run": _NamedRun(name), "plan": object(), "sources": {name: _lazy(tmp_path)}}
    assert run_staged(fn, {**call, "temp_dir": caller}, 1000) == "ok"
    assert seen["root"] in seen["staged"].parents, seen
    assert seen["outside"] == sorted([str(tmp_path / "src.parquet"), str(seen["staged"])]), seen
    assert not list(caller.parent.glob("x.typed.parquet")) and not list(elsewhere.iterdir())


def test_characterization_staged_file_name_and_mode(tmp_path: Path) -> None:
    """Characterization: the declared-cast staging keeps its file stem and 0600 mode."""
    from decoy_engine.declared_casting._stream import run_staged

    seen: dict[str, Any] = {}
    run_staged(_fn(seen), _call(tmp_path, seen), 1000)
    assert seen["path"].name == "s0000.typed.parquet"


class _ClosesThenFails:
    """A scratch sink that releases its descriptor, then reports the flush failure."""

    def __init__(self, wrapped: Any, err: int) -> None:
        self._wrapped = wrapped
        self._err = err

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def close(self) -> None:
        self._wrapped.close()
        raise OSError(self._err, "no room")


def _stage(tmp_path: Path, root: Any = None, **extra: Any) -> Any:
    from decoy_engine.declared_casting._stream import stage_typed_sources

    if root is None:
        root = tmp_path / "root"
        root.mkdir(exist_ok=True)
    return stage_typed_sources(
        ["t"],
        {"t": _lazy(tmp_path)},
        batch_fn=lambda table, batch, offset: pa.Table.from_batches([batch]),
        empty_fn=lambda table, src: src.schema.empty_table(),
        resident_fn=lambda table, value: value,
        root=root,
        batch_rows=1000,
        **extra,
    )


def _patch_sink_close(monkeypatch: pytest.MonkeyPatch, err: int) -> None:
    from decoy_engine.execution.out_of_core import _scratch

    real = _scratch.open_private
    monkeypatch.setattr(_scratch, "open_private", lambda p: _ClosesThenFails(real(p), err))


@pytest.mark.parametrize("err", [errno.ENOSPC, errno.EDQUOT])
def test_a_full_disk_on_sink_close_is_the_capacity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    from decoy_engine.execution._errors import ExecutionError

    _patch_sink_close(monkeypatch, err)
    with pytest.raises(ExecutionError) as info:
        _stage(tmp_path)
    assert str(info.value.code) == "out_of_core_temp_disk_exceeded"
    assert "root" not in str(info.value)


def test_a_non_capacity_sink_close_error_keeps_its_own_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sink_close(monkeypatch, errno.EIO)
    with pytest.raises(OSError) as info:
        _stage(tmp_path)
    assert info.value.errno == errno.EIO


def test_a_full_disk_on_writer_close_is_the_capacity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution._errors import ExecutionError

    class _WriterFull(pq.ParquetWriter):
        def close(self) -> None:
            super().close()
            raise OSError(errno.ENOSPC, "no room")

    monkeypatch.setattr(pq, "ParquetWriter", _WriterFull)
    with pytest.raises(ExecutionError) as info:
        _stage(tmp_path)
    assert str(info.value.code) == "out_of_core_temp_disk_exceeded"


def test_a_source_read_enospc_is_not_a_scratch_capacity_failure(tmp_path: Path) -> None:
    """Characterization: only scratch writes are translated; a failing source read is not."""

    def failing_read(table: str, src: Any) -> Any:
        raise OSError(errno.ENOSPC, "source side")
        yield  # pragma: no cover

    with pytest.raises(OSError) as info:
        _stage(tmp_path, open_fn=failing_read)
    assert info.value.errno == errno.ENOSPC
    assert type(info.value) is OSError


def _plain_writer_bytes(tmp_path: Path) -> bytes:
    """What one plain `ParquetWriter` writes for the source's batches, in memory."""
    sink = pa.BufferOutputStream()
    writer: Any = None
    for batch in _lazy(tmp_path).iter_batches(1000):
        table = pa.Table.from_batches([batch])
        writer = writer or pq.ParquetWriter(sink, table.schema)
        writer.write_table(table)
    writer.close()
    return bytes(sink.getvalue().to_pybytes())


@pytest.mark.parametrize("arm", ["path", "arena"])
def test_each_root_arm_stages_the_same_bytes(tmp_path: Path, arm: str) -> None:
    """The subset stage hands a path (until T18) and declared staging an arena; both stage the
    bytes a plain writer gives, in a file under the root they were handed."""
    from decoy_engine.fileaccess import ScratchArena

    expected = _plain_writer_bytes(tmp_path)
    if arm == "path":
        staged = Path(_stage(tmp_path)["t"].path)
        assert staged.parent == tmp_path / "root"
        assert staged.read_bytes() == expected
        return
    with ScratchArena.open(under=tmp_path / "arena") as arena:
        staged = Path(_stage(tmp_path, root=arena)["t"].path)
        assert staged.parent == Path(arena.local_path())
        assert staged.read_bytes() == expected
