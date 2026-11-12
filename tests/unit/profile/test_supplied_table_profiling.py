"""A supplied table with no descriptor is profiled through the same readers as a declared one.

A resident table is sampled by slicing before any pandas conversion, a supplied file handle is
read by the Parquet reader, and a value pandas cannot hold does not fail the profile.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.profile import _readers_resident, _source, profile_source
from decoy_engine.profile._readers import LazySource, ParquetFileSource
from decoy_engine.profile._readers_resident import ResidentTableSource

SAMPLE = 100


def _config(tmp_path: Path) -> dict[str, Any]:
    (tmp_path / "t.csv").write_text("a\n1\n")
    return PipelineConfig.model_validate(
        {
            "version": 2,
            "global_settings": {"seed": 1},
            "sources": {
                "t": {
                    "type": "file",
                    "format": "csv",
                    "has_header": True,
                    "path": str(tmp_path / "t.csv"),
                }
            },
            "tables": [{"name": "t", "columns": [{"name": "a", "strategy": "passthrough"}]}],
            "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
        }
    ).model_dump()


def _table_of(profile: Any, name: str) -> Any:
    return next(t for t in profile.tables if t.name == name)


def test_a_large_resident_table_is_never_converted_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    converted: list[int] = []
    real = _readers_resident._frame_of

    def counting(table: pa.Table) -> Any:
        converted.append(table.num_rows)
        return real(table)

    monkeypatch.setattr(_readers_resident, "_frame_of", counting)
    big = pa.table({"x": list(range(50_000))})
    profile = profile_source(
        _config(tmp_path), sample_rows=SAMPLE, seed=1, supplied_sources={"s": big}
    )
    s = _table_of(profile, "s")
    assert s.row_count == 50_000
    assert s.row_count_exact is True
    assert converted == [SAMPLE]


def test_a_supplied_file_handle_goes_through_the_parquet_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "s.parquet"
    pq.write_table(pa.table({"x": list(range(1_000))}), path)
    readers: list[type] = []
    real = _source._profile_one_source

    def recording(reader: Any, **kwargs: Any) -> Any:
        readers.append(type(reader))
        return real(reader, **kwargs)

    monkeypatch.setattr(_source, "_profile_one_source", recording)
    profile = profile_source(
        _config(tmp_path), sample_rows=SAMPLE, seed=1, supplied_sources={"s": LazySource(path)}
    )
    assert _table_of(profile, "s").row_count == 1_000
    assert len(readers) == 2
    assert readers[-1] is ParquetFileSource


def test_a_resident_table_goes_through_the_resident_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readers: list[type] = []
    real = _source._profile_one_source

    def recording(reader: Any, **kwargs: Any) -> Any:
        readers.append(type(reader))
        return real(reader, **kwargs)

    monkeypatch.setattr(_source, "_profile_one_source", recording)
    profile_source(_config(tmp_path), seed=1, supplied_sources={"s": pa.table({"x": [1, 2, 3]})})
    assert ResidentTableSource in readers


def test_a_date_pandas_cannot_hold_does_not_fail_the_profile(tmp_path: Path) -> None:
    far = pa.table({"d": pa.array([4_000_000, 4_000_001], pa.date32()), "n": [1, 2]})
    for residency in ("bounded", "full"):
        profile = profile_source(
            _config(tmp_path), seed=1, residency=residency, supplied_sources={"s": far}
        )
        s = _table_of(profile, "s")
        assert [c.name for c in s.columns] == ["d", "n"]
        assert s.row_count == 2
