"""The out-of-core run's scratch root is a private arena: a fresh 0700 child of a caller's
`temp_dir` (ambient only, the caller directory's own mode untouched), an engine-rooted arena when
none is named, or the arena a caller hands in, which the route uses and never reclaims.

The root is observed at the run's per-table budget walk, while the run is live. There the whole
tree is walked too (T-MODES): under any umask every directory is 0700 and every file Python created
is 0600, in both lanes, with a table that has an incoming and an outgoing FK edge.
"""

from __future__ import annotations

import gc
import os
import stat
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import fileaccess
from decoy_engine.execution.out_of_core import _runner, run_fk_out_of_core
from decoy_engine.fileaccess import ScratchArena
from decoy_engine.fileaccess._scratch import ARENA_PREFIX
from decoy_engine.plan._types import TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from tests.fileaccess.protected_dir.test_protected_dir import _Strict
from tests.parity.test_out_of_core_fk_parity import _plan as _seeded_plan
from tests.parity.test_out_of_core_fk_parity import _seed_for
from tests.unit.execution.test_out_of_core_join_chunked import _edge, _plan

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows lane is L0.1w")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _run(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> list[dict[str, Any]]:
    """Run a two-table FK job; return what each budget walk saw of the root it measured."""
    if os.geteuid() == 0:
        pytest.fail("run as a non-root user: root ignores directory modes, so this is vacuous")
    real = _runner.check_temp_disk_budget
    walks: list[dict[str, Any]] = []

    def walk(root: Path, **kw: Any) -> None:
        walks.append({"root": Path(root), "mode": _mode(Path(root))})
        real(root, **kw)

    monkeypatch.setattr(_runner, "check_temp_disk_budget", walk)
    graph = RelationshipGraph(edges=(_edge(),), ordering=())
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table({"customer_id": ["c1", "c2", "c1"], "amount": [1, 2, 3]}),
    }
    run_fk_out_of_core(
        _plan(),
        sources,
        registry=get_default_registry(),
        relationship_graph=graph,
        temp_disk_budget_bytes=1 << 40,
        **kwargs,
    )
    assert len(walks) == 2, walks  # non-vacuity: one walk per table
    return walks


def _assert_arena_child(walks: list[dict[str, Any]], caller: Path) -> None:
    roots = {w["root"] for w in walks}
    assert len(roots) == 1, roots
    (root,) = roots
    assert root.parent == caller and root.name.startswith(ARENA_PREFIX), root
    assert {w["mode"] for w in walks} == {0o700}, walks
    assert not root.exists(), "the run reclaims the arena it opened"


@pytest.mark.parametrize(
    "cell", ["mode_untouched", "missing_created", "symlink_followed", "strict_refused"]
)
def test_caller_temp_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cell: str) -> None:
    caller = tmp_path / "caller"
    if cell == "mode_untouched":
        caller.mkdir()
        os.chmod(caller, 0o755)  # noqa: S103 - a caller directory others may search
        _assert_arena_child(_run(monkeypatch, temp_dir=caller), caller)
        assert _mode(caller) == 0o755, oct(_mode(caller))
        assert list(caller.iterdir()) == []
    elif cell == "missing_created":
        _assert_arena_child(_run(monkeypatch, temp_dir=caller), caller)
        assert caller.is_dir() and _mode(caller) == 0o700
        assert list(caller.iterdir()) == []
    elif cell == "symlink_followed":
        # A symlinked caller `temp_dir` is followed, as S1a's `_open_base` does.
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        caller.symlink_to(target)
        walks = _run(monkeypatch, temp_dir=caller)
        roots = {w["root"] for w in walks}
        assert len(roots) == 1, roots
        (root,) = roots
        assert root.name.startswith(ARENA_PREFIX), root
        assert os.path.realpath(root.parent) == os.path.realpath(target), root
        assert {w["mode"] for w in walks} == {0o700}, walks
        assert caller.is_symlink()
        assert not [p for p in target.iterdir() if p.name.startswith(ARENA_PREFIX)]
    else:
        jail = tmp_path / "jail"
        jail.mkdir()
        policy = _Strict(jail)
        try:
            with fileaccess.scope(policy), pytest.raises(fileaccess.FileAccessUnavailable):
                _run(monkeypatch, temp_dir=caller)
        finally:
            policy.close()
        assert not caller.exists(), "refused before anything is created"


def test_handed_arena_is_the_route_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """A route given `_arena` spills into it and walks it: no fresh child, no reclaim."""
    with ScratchArena.open() as arena:
        opens: list[Any] = []
        real_open = ScratchArena.open

        def spy(**kw: Any) -> ScratchArena:
            opens.append(kw)
            return real_open(**kw)

        monkeypatch.setattr(ScratchArena, "open", spy)
        walks = _run(monkeypatch, _arena=arena)
        root = Path(arena.local_path())
        assert opens == [], "the route opened a second arena"
        assert {w["root"] for w in walks} == {root}, walks
        assert root.is_dir() and (root / ".decoy-arena").is_file(), "the route reclaimed it"
        assert sorted(p.name for p in root.iterdir()) == [".decoy-arena"]
    assert not root.exists()


# -- T-MODES (T01-T05): the scratch tree under any umask, both lanes ---------------------------


def fd_path(fd: int) -> str:
    """The path a directory descriptor names, to place a `dir_fd`-relative creation."""
    if sys.platform == "darwin":
        import fcntl

        return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode()
    return os.readlink(f"/proc/self/fd/{fd}")


class _Created:
    """Every directory `os.mkdir` makes and every file `os.open` creates, as absolute paths kept
    current across `os.rename` (the arena is renamed from its pending name).

    Recorded, not judged at the call: a creator restores the mode on the new descriptor after
    the umask-masked call returns, so the mode is read on the tree while the run is live."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dirs: set[str] = set()
        self.files: set[str] = set()
        real_mkdir, real_open = os.mkdir, os.open

        def at(path: Any, dir_fd: int | None) -> str:
            name = os.fsdecode(path)
            return (
                os.path.join(fd_path(dir_fd), name) if dir_fd is not None else os.path.abspath(name)
            )

        def mkdir(path: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            real_mkdir(path, mode, dir_fd=dir_fd)
            self.dirs.add(at(path, dir_fd))

        def open_(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if flags & os.O_CREAT:
                self.files.add(at(path, dir_fd))
            return fd

        def rename(src: Any, dst: Any, **fds: int | None) -> None:
            real_rename(src, dst, **fds)
            old, new = at(src, fds.get("src_dir_fd")), at(dst, fds.get("dst_dir_fd"))

            def moved(p: str) -> str:
                return new + p[len(old) :] if p == old or p.startswith(old + "/") else p

            self.dirs, self.files = {moved(p) for p in self.dirs}, {moved(p) for p in self.files}

        real_rename = os.rename
        monkeypatch.setattr(os, "mkdir", mkdir)
        monkeypatch.setattr(os, "open", open_)
        monkeypatch.setattr(os, "rename", rename)


class _MemorySink:
    """Keeps the output in memory: under umask 0777 an output stage is born 0000 (L8), and the
    sink is not what this test observes."""

    def __init__(self) -> None:
        self.tables: dict[str, pa.Table] = {}

    def write(self, table: str, data: pa.Table) -> None:
        self.tables[table] = data

    def write_batches(
        self, table: str, batches: Iterable[pa.RecordBatch], *, schema: pa.Schema
    ) -> None:
        self.tables[table] = pa.Table.from_batches(list(batches), schema=schema)

    def commit(self) -> None:
        pass

    def abort(self) -> None:
        pass


def _chain() -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    """customers -> orders -> items: `orders` has an incoming and an outgoing FK edge."""

    def seed(*columns: tuple[str, str]) -> TableSeed:
        return TableSeed(
            per_column=tuple((c, _seed_for("hash", ns)) for c, ns in columns), per_group=()
        )

    def edge(parent: str, child: str, column: str, ns: str) -> RelationshipEdge:
        return RelationshipEdge(
            parent_table=parent,
            parent_columns=(column,),
            child_table=child,
            child_columns=(column,),
            namespace=ns,
            orphan_policy=OrphanPolicy.PRESERVE,
        )

    plan = _seeded_plan(
        (
            ("customers", seed(("customer_id", "c"))),
            ("orders", seed(("order_id", "o"), ("customer_id", "c"))),
            ("items", seed(("order_id", "o"))),
        )
    )
    graph = RelationshipGraph(
        edges=(
            edge("customers", "orders", "customer_id", "c"),
            edge("orders", "items", "order_id", "o"),
        ),
        ordering=(),
    )
    sources = {
        "customers": pa.table({"customer_id": [f"c{i}" for i in range(8)]}),
        "orders": pa.table(
            {
                "order_id": [f"o{i}" for i in range(12)],
                "customer_id": [f"c{i % 9}" for i in range(12)],
            }
        ),
        "items": pa.table({"order_id": [f"o{i % 13}" for i in range(20)]}),
    }
    return plan, sources, graph


def _walk(root: Path) -> dict[str, os.stat_result]:
    """Every entry beneath `root`, and `root`, by descriptor, named as `fd_path` names them."""
    root = Path(os.path.realpath(root))
    seen = {str(root): os.stat(root, follow_symlinks=False)}
    for top, dirs, files, top_fd in os.fwalk(root):
        for name in (*dirs, *files):
            seen[os.path.join(top, name)] = os.stat(name, dir_fd=top_fd, follow_symlinks=False)
    return seen


@pytest.mark.parametrize("umask", [0o000, 0o077, 0o777], ids=lambda m: f"umask{m:04o}")
@pytest.mark.parametrize("lane", ["batch", "reorder"])
def test_ooc_scratch_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lane: str, umask: int
) -> None:
    if os.geteuid() == 0:
        pytest.fail("run as a non-root user: root ignores directory modes, so this is vacuous")
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    lanes: dict[str, str] = {}
    for attr, name in (("_stream_table", "batch"), ("stream_table", "reorder")):
        real = getattr(_runner, attr)

        def driver(*a: Any, _real: Any = real, _name: str = name, **k: Any) -> Any:
            lanes[k["table_name"]] = _name
            return _real(*a, **k)

        monkeypatch.setattr(_runner, attr, driver)
    walks: list[dict[str, os.stat_result]] = []
    real_walk = _runner.check_temp_disk_budget

    def walk(root: Path, **kw: Any) -> None:
        walks.append(_walk(Path(root)))  # before the run removes its subtrees and the arena
        real_walk(root, **kw)

    monkeypatch.setattr(_runner, "check_temp_disk_budget", walk)
    plan, sources, graph = _chain()
    sink = _MemorySink()
    created = _Created(monkeypatch)
    old = os.umask(umask)
    try:
        run_fk_out_of_core(
            plan,
            sources,
            registry=get_default_registry(),
            relationship_graph=graph,
            sink=sink,
            budget_bytes=1 << 30,
            temp_disk_budget_bytes=1 << 40,
            out_of_core_reorder_threshold_rows=0 if lane == "reorder" else None,
        )
    finally:
        os.umask(old)
        monkeypatch.undo()
    assert lanes["orders"] == lane and lanes["customers"] == "batch", lanes
    assert set(sink.tables) == {"customers", "orders", "items"}
    assert len(walks) == 3, len(walks)  # non-vacuity: one walk per table
    seen = {path: st for w in walks for path, st in w.items()}
    euid = os.geteuid()
    dirs = {p: stat.S_IMODE(st.st_mode) for p, st in seen.items() if stat.S_ISDIR(st.st_mode)}
    files = {p: stat.S_IMODE(st.st_mode) for p, st in seen.items() if stat.S_ISREG(st.st_mode)}
    assert set(seen) == set(dirs) | set(files), "only directories and regular files"
    assert {p for p, st in seen.items() if st.st_uid != euid} == set()
    assert {p: oct(m) for p, m in dirs.items() if m != 0o700} == {}, oct(umask)
    python = {p: m for p, m in files.items() if p in created.files}
    assert {p: oct(m) for p, m in python.items() if m != 0o600} == {}, oct(umask)
    # The rest are DuckDB's own files, inside its temp directory (named limit L1).
    native = set(files) - set(python)
    assert {p for p in native if "duckdb" not in Path(p).parts} == set(), native
    # Non-vacuity: the subtrees of every row this test covers were walked with Python files in them.
    names = {Path(p).name for p in python}
    assert {".decoy-arena", "masked_keys.parquet"} <= names, sorted(names)
    assert any(n.endswith("_key_relation.parquet") for n in names), sorted(names)
    rel = {str(Path(p).relative_to(Path(next(iter(walks[0]))))) for p in dirs}
    assert {"relations/t0000", "relations/t0001", "staged/t0000", "staged/t0001"} <= rel, rel


# -- the transitional Path arm owns nothing (plan 3.1.1, arm (d); S2a-1b-ii) ------------------


def _key_edge() -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="parents",
        parent_columns=("key",),
        child_table="children",
        child_columns=("key",),
        namespace="rel",
        orphan_policy=OrphanPolicy.PRESERVE,
    )


@pytest.mark.parametrize(
    "cell",
    ["reorder_spill", "relation_survives_gc", "relation_survives_gc_arena", "reclaim_refused"],
)
def test_adopted_arena_is_non_owning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cell: str
) -> None:
    """Only the opener's root reclaims: an adopted arena and a `create_dir` child, collected
    while the root is open, leave every file under them for the run that is still using it."""
    if os.geteuid() == 0:
        pytest.fail("run as a non-root user: root ignores directory modes, so this is vacuous")
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    with ScratchArena.open() as root:
        base = Path(root.local_path())
        if cell == "reorder_spill":
            _reorder_spill(base / "joins" / "t0001" / "duckdb")  # the leaf the reorder joiner names
        elif cell == "reclaim_refused":
            with ScratchArena._adopt(tmp_path / "adopted") as adopted:
                with pytest.raises(fileaccess.FileAccessUnavailable):
                    adopted.reclaim()
                with pytest.raises(fileaccess.FileAccessUnavailable):
                    adopted.create_dir("c").reclaim()
            assert (tmp_path / "adopted" / "c").is_dir()
        else:
            _relation_survives(root, base, cell)
        assert (base / ".decoy-arena").is_file()
    assert not base.exists()


def _reorder_spill(leaf: Path) -> None:
    from decoy_engine.execution.out_of_core._duckdb import connect_duckdb

    old = os.umask(0o022)
    try:
        adopted = ScratchArena._adopt(leaf)  # arm (c)'s shape: a missing leaf made 0700
        conn = connect_duckdb(temp_dir=Path(adopted.local_path()), memory_limit="16MB")
        try:
            conn.execute("SET preserve_insertion_order = false")
            conn.execute(
                "CREATE TEMP TABLE spill AS SELECT range AS i, repeat('x', 96) AS s"
                " FROM range(600000)"
            )
            spilled = [Path(r[0]) for r in conn.execute("FROM duckdb_temporary_files()").fetchall()]
            assert spilled and {p.parent for p in spilled} == {leaf}, spilled  # non-vacuity
            del adopted
            gc.collect()  # the spill must outlive the adopted arena object
            assert all(p.is_file() for p in spilled), spilled
            total = conn.execute("SELECT count(*), sum(length(s)) FROM spill").fetchone()
            assert total == (600000, 600000 * 96)
            assert _mode(leaf) == _mode(leaf.parent) == 0o700
        finally:
            conn.close()
    finally:
        os.umask(old)


def _relation_survives(root: ScratchArena, base: Path, cell: str) -> None:
    from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
    from decoy_engine.execution.out_of_core._relation import build_parent_key_relation_aligned
    from decoy_engine.execution.out_of_core._stage import MaskedKeyStager

    edge = _key_edge()
    parent = pa.table({"key": ["c1", "c2"]})
    masked = pa.table({"key": ["m1", "m2"]})
    stager = MaskedKeyStager(  # the Path arm (arm (d)), adopting staged/t0000
        base / "staged" / "t0000" / "masked_keys.parquet",
        columns=("key",),
        fixed_schema=masked.schema,
    )
    batch = masked.to_batches()[0]
    stager.add(batch, batch)
    stager.close()
    relations = None if cell == "relation_survives_gc" else root.create_dir("relations")
    holder = (
        ScratchArena._adopt(base / "relations" / "t0000")
        if relations is None
        else relations.create_dir("t0000")
    )
    relation = build_parent_key_relation_aligned(
        source_parent=parent,
        masked_parent=stager.source(),
        edge=edge,
        temp_dir=Path(holder.local_path()),
    )
    del holder, relations, stager
    gc.collect()
    assert relation.path.is_file() and relation.path.name.endswith("_key_relation.parquet")
    child = pa.table({"key": ["c2", "c1", "x"]}).to_batches()[0]
    with ChildFkBatchJoiner(
        edge=edge, parent_relation=relation, child_key_types=(pa.string(),), temp_dir=base / "j"
    ) as joiner:
        out, orphans = joiner.join_batch(child)
    assert out.column(0).to_pylist() == ["m2", "m1", "x"] and orphans == 1


# -- connections on the arena (T05, arm (c); S2a-1c) -------------------------------------------


@pytest.mark.parametrize("arm", ["path", "arena"])
def test_connect_duckdb_spills_into_its_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arm: str
) -> None:
    """`temp_directory` is the handed arena child, or (arm (c)) the adopted path: 0700 under
    umask 0777, made before DuckDB is asked to spill there."""
    from decoy_engine.execution.out_of_core._duckdb import connect_duckdb

    if os.geteuid() == 0:
        pytest.fail("run as a non-root user: root ignores directory modes, so this is vacuous")
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    with ScratchArena.open() as root:
        spill = Path(root.local_path()) / "joins" / "duckdb"
        old = os.umask(0o777)
        try:
            where = root.create_dir("joins").create_dir("duckdb") if arm == "arena" else spill
            conn = connect_duckdb(temp_dir=where, memory_limit="64MB")
        finally:
            os.umask(old)
        try:
            (setting,) = conn.execute("SELECT current_setting('temp_directory')").fetchone()
        finally:
            conn.close()
        assert Path(setting) == spill and _mode(spill) == 0o700, (setting, oct(_mode(spill)))


def test_connect_duckdb_refuses_a_loose_spill_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An existing spill directory others can enter is refused, value-free, and left as it was:
    a directory the engine did not create is checked, never chmodded (plan 4.2, arm (c))."""
    from decoy_engine.execution.out_of_core._duckdb import connect_duckdb

    if os.geteuid() == 0:
        pytest.fail("run as a non-root user: root ignores directory modes, so this is vacuous")
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    with ScratchArena.open() as root:
        loose = Path(root.local_path()) / "patients_tbl"
        loose.mkdir()
        os.chmod(loose, 0o755)  # noqa: S103 - the loose directory under test
        with pytest.raises(ProtectedDirUnsafe) as info:
            connect_duckdb(temp_dir=loose, memory_limit="64MB")
        assert "patients_tbl" not in f"{info.value!s} {info.value!r}", repr(info.value)
        assert _mode(loose) == 0o755, oct(_mode(loose))
        assert list(loose.iterdir()) == []
