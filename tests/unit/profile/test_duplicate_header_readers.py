"""T12: what each reader hands the engine for a duplicate header (`a,a,b`).

Plan `docs/plans/2026-10-31-slice0-derived-visibility.md` section 4.1. The duplicate-name
refusal covers readers that preserve duplicates; pandas-read CSV renames them (`a`, `a.1`)
and is out of the guarantee by measurement, not by assertion. One cell per reader row, so a
reader that changes behavior turns its cell red instead of the claim silently going false.
Labels: R1-R5, R7, R13 and R14 CHARACTERIZATION; R6 FAIL-BEFORE.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests._declared_schema import declare_rest

DUP_CSV = b"a,a,b\n1,2,x\n3,4,y\n"
RENAMED = ["a", "a.1", "b"]
KEPT = ["a", "a", "b"]


def _dup_table() -> pa.Table:
    return pa.Table.from_arrays(
        [pa.array([1, 3]), pa.array([2, 4]), pa.array(["x", "y"])], names=KEPT
    )


def _dup_parquet(tmp_path: Path) -> Path:
    path = tmp_path / "dup.parquet"
    pq.write_table(_dup_table(), path)
    return path


# ------------------------------------------------------------------ R1 local CSV


def test_r1_local_csv_renames_a_repeated_header(tmp_path: Path) -> None:
    from decoy_engine.profile._readers import build_profile_source

    path = tmp_path / "dup.csv"
    path.write_bytes(DUP_CSV)
    src = build_profile_source(
        {"type": "file", "format": "csv", "path": str(path), "has_header": True}
    )
    assert src.schema().names == RENAMED
    assert list(src.sample_frame(10).columns) == RENAMED
    assert list(src.to_frame().columns) == RENAMED


# ------------------------------------------------------------------ R2 cloud CSV (stubbed)


class _Body(io.BytesIO):
    pass


class _FakeS3:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        return {"ContentLength": len(self._data)}

    def get_object(self, Bucket: str, Key: str, Range: str | None = None) -> dict[str, Any]:
        if Range is None:
            return {"Body": _Body(self._data)}
        start, end = map(int, re.match(r"bytes=(\d+)-(\d+)", Range).groups())  # type: ignore[union-attr]
        return {"Body": _Body(self._data[start : end + 1])}


class _FakeBlob:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.size = len(data)

    def reload(self) -> None: ...

    def download_as_bytes(
        self, start: int | None = None, end: int | None = None, **sdk_options: Any
    ) -> bytes:
        # Real google-cloud-storage accepts `timeout`, `retry` and more; the engine passes some.
        if start is None:
            return self._data
        return self._data[start : (end or len(self._data) - 1) + 1]


class _FakeGcsClient:
    def __init__(self, data: bytes) -> None:
        self._blob = _FakeBlob(data)

    def __enter__(self) -> _FakeGcsClient:
        return self

    def __exit__(self, *exc: Any) -> None: ...

    def bucket(self, name: str) -> Any:
        blob = self._blob

        class _Bucket:
            def blob(self, object_name: str) -> _FakeBlob:
                return blob

        return _Bucket()


def _stub_cloud(monkeypatch: pytest.MonkeyPatch, data: bytes, kind: str) -> None:
    from decoy_engine.profile import _cloud_readers

    if kind == "s3":
        monkeypatch.setattr(_cloud_readers._clients, "s3_client", lambda **_kw: _FakeS3(data))
        return
    storage = pytest.importorskip("google.cloud.storage")
    monkeypatch.setattr(storage, "Client", lambda *a, **k: _FakeGcsClient(data))


def test_r2_cloud_csv_sample_helper_renames() -> None:
    from decoy_engine.profile._cloud_readers import _csv_sample_from_window

    assert list(_csv_sample_from_window(DUP_CSV, len(DUP_CSV), 10).columns) == RENAMED


@pytest.mark.parametrize("kind", ["s3", "gcs"])
def test_r2_cloud_csv_sources_rename(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.profile._readers import build_profile_source

    _stub_cloud(monkeypatch, DUP_CSV, kind)
    descriptor: dict[str, Any] = {"type": kind, "format": "csv", "bucket": "b"}
    descriptor["key" if kind == "s3" else "object"] = "dup.csv"
    src = build_profile_source(descriptor)
    assert src.schema().names == RENAMED
    assert list(src.sample_frame(10).columns) == RENAMED
    assert list(src.to_frame().columns) == RENAMED


# ------------------------------------------------------------------ R3 headerless CSV


def test_r3_headerless_repeated_column_names_are_refused_by_config(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from decoy_engine.config import PipelineConfig
    from decoy_engine.profile._readers_csv import CsvFileSource

    path = tmp_path / "dup.csv"
    path.write_bytes(b"1,2,x\n3,4,y\n")
    with pytest.raises(ValidationError, match="column_names` must be unique"):
        PipelineConfig.model_validate(
            {
                "version": 2,
                "sources": {
                    "t": {
                        "type": "file",
                        "format": "csv",
                        "path": str(path),
                        "has_header": False,
                        "column_names": KEPT,
                    }
                },
                "tables": [{"name": "t", "columns": [{"name": "b", "strategy": "redact"}]}],
                "targets": {"t": {"type": "file", "format": "csv", "path": str(path) + ".out"}},
            }
        )
    # The dialect refuses the repeat on a direct reader too (main's CsvDialect rule), so the
    # repeated names never reach a frame or a schema.
    from decoy_engine.errors import ConfigError

    direct = CsvFileSource(path, has_header=False, column_names=KEPT)
    with pytest.raises(ConfigError, match="column_names` must be unique"):
        direct.sample_frame(10)
    with pytest.raises(ConfigError, match="column_names` must be unique"):
        direct.schema()


# ------------------------------------------------------------------ R4 fixed width


def test_r4_fixed_width_repeated_name_is_refused(tmp_path: Path) -> None:
    from decoy_engine.config._fixed_width import FixedWidthLayout

    with pytest.raises(Exception, match="duplicate column name 'a'"):
        FixedWidthLayout.model_validate(
            {
                "columns": [
                    {"name": "a", "start": 0, "width": 1},
                    {"name": "a", "start": 1, "width": 1},
                ]
            }
        )


# ------------------------------------------------------------------ R5, R7 Parquet


def test_r5_local_parquet_schema_keeps_the_duplicate(tmp_path: Path) -> None:
    from decoy_engine.profile._readers import build_profile_source

    path = _dup_parquet(tmp_path)
    src = build_profile_source({"type": "file", "format": "parquet", "path": str(path)})
    assert src.schema().names == KEPT


def test_r7_lazy_source_keeps_the_duplicate_and_cannot_materialize(tmp_path: Path) -> None:
    from decoy_engine.profile._readers import LazySource

    lazy = LazySource(path=_dup_parquet(tmp_path))
    assert lazy.schema.names == KEPT
    with pytest.raises(pa.ArrowInvalid, match="duplicate field names"):
        lazy.to_table()


# ------------------------------------------------------------------ R6 cloud Parquet descriptor


@pytest.mark.parametrize("kind", ["s3", "gcs"])
def test_r6_cloud_parquet_descriptor_is_refused_before_profiling(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FAIL-BEFORE (main: the profile reads the duplicate frame and walk_dataframe raises).
    import decoy_engine.profile as profile_pkg
    from decoy_engine import run_pipeline
    from decoy_engine.execution._transforms import TransformError
    from tests._keyed_secret import THROWAWAY_SECRET
    from tests.unit.execution._transform_testkit import ENGINE_VERSION, validated

    path = _dup_parquet(tmp_path)
    _stub_cloud(monkeypatch, path.read_bytes(), kind)
    profiled: list[int] = []
    real_profile = profile_pkg.profile_source
    monkeypatch.setattr(
        profile_pkg, "profile_source", lambda *a, **k: profiled.append(1) or real_profile(*a, **k)
    )
    monkeypatch.setenv("DECOY_TEST_THROWAWAY_SECRET", THROWAWAY_SECRET.hex())
    descriptor: dict[str, Any] = {"type": kind, "format": "parquet", "bucket": "b"}
    descriptor["key" if kind == "s3" else "object"] = "dup.parquet"
    cfg = validated(
        {
            "version": 2,
            "global_settings": {"seed": 3, "mask_secret_ref": "env:DECOY_TEST_THROWAWAY_SECRET"},
            "sources": {"t": descriptor},
            "tables": [{"name": "t", "columns": [{"name": "b", "strategy": "redact"}]}],
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "o.parquet")}
            },
        }
    )
    clean = pa.table({"a": [1, 3], "b": ["x", "y"]})
    declare_rest(cfg, {"t": clean})
    with pytest.raises(Exception) as exc:
        run_pipeline(cfg, {"t": clean}, engine_version=ENGINE_VERSION)
    assert type(exc.value) is TransformError
    assert exc.value.code == "duplicate_source_field_names"
    assert str(exc.value) == "table 't' has 1 duplicated field name(s)"
    assert profiled == []


# ------------------------------------------------------------------ R13, R14 streaming readers


def test_r13_engine_streaming_readers_keep_the_duplicate(tmp_path: Path) -> None:
    from decoy_engine.io import ChunkBudget, iter_csv_chunks, iter_parquet_chunks

    csv_path = tmp_path / "dup.csv"
    csv_path.write_bytes(DUP_CSV)
    budget = ChunkBudget(chunk_budget_bytes=1 << 20, max_chunk_rows=1, max_row_bytes=1024)
    assert [c.column_names for c in iter_csv_chunks(str(csv_path), budget, table="t")] == [
        KEPT,
        KEPT,
    ]
    pq_path = _dup_parquet(tmp_path)
    assert [c.column_names for c in iter_parquet_chunks(str(pq_path), budget, table="t")] == [
        KEPT,
        KEPT,
    ]


_PA_CSV = re.compile(
    r"^\s*(import pyarrow\.csv\b|from pyarrow import (.*\b)?csv\b|from pyarrow\.csv import )",
    re.MULTILINE,
)


def test_r14_only_the_bounded_reader_imports_pyarrow_csv() -> None:
    """Ratchet: a new `pyarrow.csv` importer preserves duplicates and must be added to the
    reader table and to a covered path."""
    src = Path(__file__).parents[3] / "src" / "decoy_engine"
    importers = {
        str(p.relative_to(src)) for p in src.rglob("*.py") if _PA_CSV.search(p.read_text("utf-8"))
    }
    assert importers == {"io/bounded_reader.py"}
