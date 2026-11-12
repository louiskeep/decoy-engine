"""The typed staging copy holds raw source rows, so it is 0600 from creation, whatever the umask.

The mode is read while the copy is being written (at each later batch's cast), not only after
it is closed, so a chmod applied after the rows land does not pass.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

UMASKS = (0o000, 0o077, 0o777)


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


def _modes(root: Path) -> dict[str, int]:
    return {
        str(p.relative_to(root)): stat.S_IMODE(p.lstat().st_mode)
        for p in root.rglob("*.typed.parquet")
    }


class _Run:
    """Records the staged file's mode at every cast, i.e. after each earlier batch was written."""

    def __init__(self, caller: Path) -> None:
        self.caller = caller
        self.during: list[dict[str, int]] = []

    def tables(self) -> list[str]:
        return ["t"]

    def cast_batch(self, table: str, batch: Any, offset: int) -> Any:
        self.during.append(_modes(self.caller))
        if isinstance(batch, pa.RecordBatch):
            batch = pa.Table.from_batches([batch])
        return SimpleNamespace(table=batch)


def _lazy(tmp_path: Path, rows: int) -> Any:
    from decoy_engine.profile._readers import LazySource

    path = tmp_path / "src.parquet"
    pq.write_table(pa.table({"v": pa.array([str(i) for i in range(rows)], pa.string())}), path)
    return LazySource(path)


@pytest.mark.parametrize("rows", [5000, 0], ids=["rows", "zero_rows"])
def test_staged_copy_is_0600_while_written_and_after(
    tmp_path: Path, umask_value: int, rows: int
) -> None:
    from decoy_engine.declared_casting._stream import run_staged

    source = _lazy(tmp_path, rows)  # an input: written before the umask applies
    caller = tmp_path / "caller-root"
    run = _Run(caller)
    seen: dict[str, Any] = {}

    def fn(plan: Any, sources: Any, *, temp_dir: Path, **rest: Any) -> str:
        seen["root"] = stat.S_IMODE(Path(temp_dir).lstat().st_mode)
        seen["after"] = _modes(Path(temp_dir))
        seen["rows"] = sources["t"].schema is not None
        return "ok"

    call = {"cast_run": run, "plan": object(), "sources": {"t": source}, "temp_dir": caller}
    # The engine runs, and creates the missing caller-root, under the umask.
    with _umask(umask_value) as umask:
        assert run_staged(fn, call, 1000) == "ok"

    assert len(seen["after"]) == 1, seen  # non-vacuity: exactly one staged copy was observed
    assert set(seen["after"].values()) == {0o600}, (oct(umask), seen["after"])
    assert seen["root"] == 0o700, (oct(umask), oct(seen["root"]))
    if rows:
        during = [m for m in run.during if m]
        assert len(during) >= 4, run.during  # non-vacuity: observed mid-write, several batches
        assert all(set(m.values()) == {0o600} for m in during), (oct(umask), during)
