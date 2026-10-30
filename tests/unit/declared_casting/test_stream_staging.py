"""The typed staging copy of an out-of-core declared source honours the caller's temp root and
counts against the temp-disk budget, like every other spill of the run."""

from __future__ import annotations

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
