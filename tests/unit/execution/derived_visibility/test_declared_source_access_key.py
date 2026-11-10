"""The run entry's duplicate-name check reads a declared source under its own config position.

A policy that grants file access per position (`PreopenedFileAccess`) keys a source by
`sources.<table>.path`. The entry check must ask for that key, the same one profiling asks
for, or a run with a valid grant is refused before it starts.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import fileaccess, run_pipeline
from decoy_engine.execution._transforms import TransformError
from decoy_engine.fileaccess import FileRole
from tests._declared_schema import declare_rest
from tests.unit.execution._transform_testkit import ENGINE_VERSION


def _config(path: Path) -> dict[str, object]:
    return declare_rest(
        {
            "version": 1,
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "tables": [],
            "targets": {"t": {"type": "file", "format": "parquet", "path": f"{path}.out"}},
        }
    )


@pytest.fixture
def opened() -> Iterator[list[int]]:
    fds: list[int] = []
    yield fds
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _policy(path: Path, fds: list[int]) -> fileaccess.PreopenedFileAccess:
    fd = os.open(path, os.O_RDONLY)
    fds.append(fd)
    return fileaccess.PreopenedFileAccess(files={(FileRole.SOURCE, "sources.t.path"): fd})


def test_a_unique_parquet_source_runs_under_a_per_position_policy(
    tmp_path: Path, opened: list[int]
) -> None:
    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"a": [1, 2]}), path)
    run_pipeline(_config(path), file_access=_policy(path, opened), engine_version=ENGINE_VERSION)


def test_a_repeated_name_parquet_source_is_refused_under_a_per_position_policy(
    tmp_path: Path, opened: list[int]
) -> None:
    path = tmp_path / "t.parquet"
    pq.write_table(pa.Table.from_arrays([pa.array([1]), pa.array([2])], names=["a", "a"]), path)
    with pytest.raises(TransformError) as caught:
        run_pipeline(
            _config(path), file_access=_policy(path, opened), engine_version=ENGINE_VERSION
        )
    assert caught.value.code == "duplicate_source_field_names"
