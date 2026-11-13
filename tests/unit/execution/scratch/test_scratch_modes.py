"""The out-of-core run's scratch root is a private arena: a fresh 0700 child of a caller's
`temp_dir` (ambient only, the caller directory's own mode untouched), an engine-rooted arena when
none is named, or the arena a caller hands in, which the route uses and never reclaims.

The root is observed at the run's per-table budget walk, while the run is live.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import fileaccess
from decoy_engine.execution.out_of_core import _runner, run_fk_out_of_core
from decoy_engine.fileaccess import ScratchArena
from decoy_engine.fileaccess._scratch import ARENA_PREFIX
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from tests.fileaccess.protected_dir.test_protected_dir import _Strict
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
