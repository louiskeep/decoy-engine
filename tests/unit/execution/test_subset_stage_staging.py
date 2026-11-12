"""AT-32: the one staging writer. Subset staging shares `stage_typed_sources` with declared casting."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.declared_casting import stage_typed_sources
from decoy_engine.profile._readers import LazySource


def _lazy(tmp_path: Path, rows: int = 30) -> LazySource:
    path = tmp_path / "src.parquet"
    pq.write_table(pa.table({"v": [f"v{i}" for i in range(rows)]}), path)
    return LazySource(path)


def _stage(tmp_path: Path, src: Any, **kw: Any) -> dict[str, Any]:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    args: dict[str, Any] = {
        "batch_fn": lambda t, b, off: pa.Table.from_batches([b]),
        "empty_fn": lambda t, s: s.schema.empty_table(),
        "resident_fn": lambda t, v: v,
        "root": root,
        "batch_rows": 10,
        "stem": "subset",
    }
    args.update(kw)
    return stage_typed_sources(["t"], {"t": src}, **args)


def test_staged_file_is_0600_under_umask_000_and_named_by_stem(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        out = _stage(tmp_path, _lazy(tmp_path))
    finally:
        os.umask(old)
    path = Path(out["t"].path)
    assert path.name == "s0000.subset.parquet"
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_zero_row_source_gets_a_typed_empty_file(tmp_path: Path) -> None:
    out = _stage(tmp_path, _lazy(tmp_path, rows=0))
    assert out["t"].num_rows == 0 and out["t"].schema.names == ["v"]


def test_a_budget_failure_leaves_the_scope_to_clean_up(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError

    with pytest.raises(ExecutionError) as info:
        _stage(tmp_path, _lazy(tmp_path), max_bytes=1)
    assert info.value.code == "out_of_core_temp_disk_exceeded"


def test_two_stems_never_share_a_file_name(tmp_path: Path) -> None:
    a = _stage(tmp_path, _lazy(tmp_path), stem="typed")
    b = _stage(tmp_path, _lazy(tmp_path), stem="subset")
    assert Path(a["t"].path) != Path(b["t"].path)
