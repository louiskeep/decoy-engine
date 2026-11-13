"""A declared out-of-core job keeps its typed staging copy and the route's spill in ONE arena,
measured against ONE `temp_disk_budget_bytes` (F25 T18 hand-over).

The route's spill is a fixed-size file written into the route's scratch root at its first table
(as a seam around the route), so it persists at the table boundary where the budget is walked. The
`QuotaReport` on the budget error is not asserted here; it lands with the report arms.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import fileaccess
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import _runner, run_fk_out_of_core
from decoy_engine.fileaccess._scratch import ARENA_PREFIX
from decoy_engine.providers_v2 import get_default_registry
from tests._keyed_secret import THROWAWAY_KEY_PROVIDER
from tests.fileaccess.scratch.test_scratch_plain_host import (
    F06_ROWS,
    _digest,
    _IdentityCast,
    _lazy,
)
from tests.fileaccess.support import RootRelative
from tests.fileaccess.test_x4_scratch_and_names import PLAIN, _jobs

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows cells are S1w")


@pytest.fixture(autouse=True)
def temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen: dict[str, Any],
    *,
    spill: int,
    budget: int,
    sink: bool = True,
    **extra: Any,
) -> Any:
    """Run the declared job, into a sink unless `sink` is false; record the route's root, its
    staged sources and the typed copies' bytes at the route's first table."""
    for name in ("_stream_table", "stream_table"):
        real = getattr(_runner, name)

        def route(*a: Any, _real: Any = real, **k: Any) -> Any:
            joins = k["temp_dir"]  # the batch lane takes an arena child, the reorder lane a path
            root = Path(joins if isinstance(joins, Path) else joins.local_path()).parents[1]
            if "root" not in seen:
                seen["root"] = root
                staged = Path(k["raw"].path).parent
                seen["copy"] = sum(p.stat().st_size for p in staged.glob("*.typed.parquet"))
                (root / "spill.bin").write_bytes(os.urandom(spill))
            seen.setdefault("staged", []).append(Path(k["raw"].path))
            return _real(*a, **k)

        monkeypatch.setattr(_runner, name, route)
    real_walk = _runner.check_temp_disk_budget

    def walk(root: Path, **kw: Any) -> None:
        own = [p for p in Path(root).rglob("*") if p.is_file()]
        own = [p for p in own if p.name != "spill.bin" and not p.name.endswith(".typed.parquet")]
        seen["own"] = max(seen.get("own", 0), sum(p.stat().st_size for p in own))
        real_walk(root, **kw)

    monkeypatch.setattr(_runner, "check_temp_disk_budget", walk)
    plan, sources, graph = _jobs(*PLAIN)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return run_fk_out_of_core(
        plan,
        _lazy(tmp_path, sources),
        registry=get_default_registry(),
        relationship_graph=graph,
        key_provider=THROWAWAY_KEY_PROVIDER,
        cast_run=_IdentityCast(set(sources)),
        sink=ParquetTransactionalSink(out / "result") if sink else None,
        temp_disk_budget_bytes=budget,
        **extra,
    )


def _probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    """The typed copies' bytes, and the largest footprint of the route's own files at a walk."""
    seen: dict[str, Any] = {}
    _job(tmp_path / "probe", monkeypatch, seen, spill=0, budget=1 << 40)
    assert seen["copy"] > 0 and seen["own"] > 0, seen
    return int(seen["copy"]), int(seen["own"])


@pytest.mark.parametrize("cell", ["ambient_temp_dir", "no_temp_dir", "strict_policy"])
def test_declared_ooc_one_arena_one_budget(
    tmp_path: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch, cell: str
) -> None:
    (tmp_path / "probe").mkdir()
    copy, own = _probe(tmp_path, monkeypatch)
    # One root holds the copy, the route's own files and a spill the copy's size: over budget.
    # A route root apart from the copy would hold only its own files and the spill: under it.
    budget = copy + own + copy // 2
    seen: dict[str, Any] = {}
    caller = tmp_path / "caller"
    parent = caller if cell == "ambient_temp_dir" else temp_root
    extra: dict[str, Any] = {"temp_dir": caller} if cell == "ambient_temp_dir" else {}
    if cell == "strict_policy":
        policy = RootRelative(tmp_path)
        try:
            with fileaccess.scope(policy):
                result = _job(
                    tmp_path, monkeypatch, seen, spill=copy, budget=4 * (copy + own), sink=False
                )
        finally:
            policy.close()
        assert _digest(result.outputs) == F06_ROWS  # the sink path needs a policy-issued stage
    else:
        with pytest.raises(ExecutionError) as info:
            _job(tmp_path, monkeypatch, seen, spill=copy, budget=budget, **extra)
        assert str(info.value.code) == "out_of_core_temp_disk_exceeded"
        assert not (tmp_path / "out" / "result").exists(), "nothing is committed"
    root = seen["root"]
    assert root.parent == parent and root.name.startswith(ARENA_PREFIX), root
    assert {p.parent for p in seen["staged"]} == {root}, seen  # the copy is in the route's root
    assert not root.exists(), "the arena is reclaimed"
    assert not [p for p in parent.iterdir() if p.name.startswith("decoy-arena")]
