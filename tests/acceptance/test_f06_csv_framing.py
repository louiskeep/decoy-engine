"""F06 r2: CSV readers must not destroy a declared sentinel before the casting boundary.

A declared column keeps its exact lexeme (``NA`` stays ``"NA"``, an empty field stays ``""``)
on every CSV reader; undeclared columns keep the legacy NA vocabulary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tests.acceptance._f06_support import INT64, column, config, run, write_csv

SENTINEL = {"id": "sentinel/exact/v1", "values": ["NA"]}


def _cfg(tmp_path: Path) -> dict[str, Any]:
    src = write_csv(
        tmp_path / "t.csv",
        ["id", "age", "note"],
        [["1", "NA", "NA"], ["2", "042", "x"], ["3", "", ""]],
    )
    return config(
        [
            column("id"),
            column(
                "age",
                logical_type="integer",
                parse_format=INT64,
                sentinel_to_null=SENTINEL,
                error_policy="null",
            ),
            column("note"),
        ],
        source_path=src,
        table="t",
    )


def test_declared_columns_are_derived_from_the_config(tmp_path: Path) -> None:
    from decoy_engine._lexeme import lexeme_columns_by_table
    from tests.acceptance._f06_support import validate

    assert lexeme_columns_by_table(validate(_cfg(tmp_path))) == {"t": frozenset({"age"})}


def test_local_csv_profile_reader_keeps_declared_lexemes(tmp_path: Path) -> None:
    from decoy_engine._lexeme import profile_descriptors
    from decoy_engine.profile._readers import build_profile_source
    from tests.acceptance._f06_support import validate

    cfg = validate(_cfg(tmp_path))
    frame = build_profile_source(profile_descriptors(cfg)["t"]).to_frame()
    assert frame["age"].tolist() == ["NA", "042", ""]  # lexemes, not NaN / int 42
    assert frame["note"].isna().tolist() == [True, False, True]  # legacy NA vocabulary intact


def test_profile_source_receives_the_contract_aware_descriptors(tmp_path: Path) -> None:
    from decoy_engine.profile import profile_source
    from tests.acceptance._f06_support import validate

    profile = profile_source(validate(_cfg(tmp_path)), seed=7)
    assert profile.tables[0].name == "t"


def test_bounded_reader_keeps_declared_lexemes(tmp_path: Path) -> None:
    from decoy_engine.io.bounded_reader import ChunkBudget, iter_csv_chunks

    path = tmp_path / "b.csv"
    path.write_text("age,note\nNA,NA\n042,x\n,\n", encoding="utf-8")
    budget = ChunkBudget(chunk_budget_bytes=1 << 20, max_chunk_rows=100, max_row_bytes=1 << 16)
    (chunk,) = list(
        iter_csv_chunks(str(path), budget, table="t", lexeme_columns=frozenset({"age"}))
    )
    assert chunk.column("age").to_pylist() == ["NA", "042", ""]
    assert chunk.column("note").to_pylist() == [None, "x", None]
    legacy = next(iter_csv_chunks(str(path), budget, table="t"))
    assert legacy.column("age").to_pylist() == [None, "042", None]  # undeclared: unchanged


CLOUD_CSV = b"age,note\nNA,NA\n042,x\n,\n"


def _cloud_cfg(kind: str) -> dict[str, Any]:
    """A validated config whose only source is a cloud CSV with one declared column."""
    location = (
        {"type": "s3", "bucket": "b", "key": "t.csv"}
        if kind == "s3"
        else {"type": "gcs", "bucket": "b", "object": "t.csv"}
    )
    return {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {"t": {**location, "format": "csv"}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    column("note"),
                    column(
                        "age",
                        logical_type="integer",
                        parse_format=INT64,
                        sentinel_to_null=SENTINEL,
                        error_policy="null",
                    ),
                ],
            }
        ],
        "targets": {"t": {"type": "file", "format": "csv", "path": "/tmp/f06-out.csv"}},
        "relationships": [],
        "namespaces": {},
    }


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _Body:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    def head_object(self, Bucket: str, Key: str) -> dict[str, int]:
        return {"ContentLength": len(CLOUD_CSV)}

    def get_object(self, Bucket: str, Key: str, Range: str | None = None) -> dict[str, Any]:
        if Range is None:
            return {"Body": _Body(CLOUD_CSV)}
        _, _, span = Range.partition("=")
        start, _, end = span.partition("-")
        return {"Body": _Body(CLOUD_CSV[int(start) : int(end) + 1])}


class _FakeBlob:
    size = len(CLOUD_CSV)

    def download_as_bytes(self, start: int | None = None, end: int | None = None) -> bytes:
        return CLOUD_CSV if start is None else CLOUD_CSV[start : (end or 0) + 1]


class _FakeGcs:
    def __enter__(self) -> _FakeGcs:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def bucket(self, name: str) -> Any:
        return type("_Bucket", (), {"blob": lambda self, obj: _FakeBlob()})()


@pytest.fixture
def cloud_reader(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real reader class for ``kind``, built through its production builder, over an
    in-memory client (the only fake is the network)."""
    from decoy_engine._lexeme import profile_descriptors
    from decoy_engine.profile import _cloud_readers as cr
    from tests.acceptance._f06_support import validate

    def build(kind: str) -> Any:
        descriptor = profile_descriptors(validate(_cloud_cfg(kind)))["t"]
        if kind == "s3":
            monkeypatch.setattr(cr, "_s3_client", lambda _descriptor: _FakeS3())
            reader = cr.build_s3_profile_source(descriptor)
            assert isinstance(reader, cr.S3CsvSource)
            return reader
        try:
            import google.cloud.storage
        except ImportError:  # the optional client is absent; the reader only needs `Client()`
            import sys
            import types

            google = types.ModuleType("google")
            cloud = types.ModuleType("google.cloud")
            storage = types.ModuleType("google.cloud.storage")
            google.cloud = cloud  # type: ignore[attr-defined]
            cloud.storage = storage  # type: ignore[attr-defined]
            for name, mod in (
                ("google", google),
                ("google.cloud", cloud),
                ("google.cloud.storage", storage),
            ):
                monkeypatch.setitem(sys.modules, name, mod)
        monkeypatch.setattr(
            "google.cloud.storage.Client", lambda *a, **k: _FakeGcs(), raising=False
        )
        reader = cr.build_gcs_profile_source(descriptor)
        assert isinstance(reader, cr.GcsCsvSource)
        return reader

    return build


@pytest.mark.parametrize("kind", ["s3", "gcs"])
def test_cloud_csv_readers_honour_lexeme_columns(cloud_reader: Any, kind: str) -> None:
    reader = cloud_reader(kind)
    sample = reader.sample_frame(10)
    full = reader.to_frame()
    for frame in (sample, full):
        assert frame["age"].tolist() == ["NA", "042", ""]  # exact lexemes, not NaN / int 42
        assert frame["note"].isna().tolist() == [True, False, True]  # undeclared: legacy NA
    assert str(reader.schema().field("age").type) in {"string", "large_string"}


@pytest.mark.parametrize("kind", ["s3", "gcs"])
def test_cloud_csv_reader_loses_the_lexeme_when_descriptor_plumbing_breaks(
    cloud_reader: Any, kind: str
) -> None:
    """The same reader without the contract-aware descriptor keeps the legacy NA behaviour, so
    a regression that stops attaching ``lexeme_columns`` turns the test above red."""
    from decoy_engine._lexeme import LEXEME_KEY

    reader = cloud_reader(kind)
    reader._descriptor.pop(LEXEME_KEY)
    assert reader.to_frame()["age"].tolist() != ["NA", "042", ""]


def test_end_to_end_csv_sentinel_is_counted_not_a_source_null(tmp_path: Path) -> None:
    """The declared ``NA`` sentinel must reach F06 as ``NA``: one sentinel_to_null, and the
    empty field is a parse failure under ``null`` (empty is a sentinel only if listed)."""
    from decoy_engine._lexeme import lexeme_columns_by_table, profile_descriptors
    from decoy_engine.io.bounded_reader import ChunkBudget, iter_csv_chunks
    from tests.acceptance._f06_support import validate

    cfg = _cfg(tmp_path)
    lex = lexeme_columns_by_table(validate(cfg))
    budget = ChunkBudget(chunk_budget_bytes=1 << 20, max_chunk_rows=100, max_row_bytes=1 << 16)
    chunks = list(
        iter_csv_chunks(cfg["sources"]["t"]["path"], budget, table="t", lexeme_columns=lex["t"])
    )
    table = pa.concat_tables(chunks)
    result = run(cfg, {"t": table})
    counts = result.quality_metrics["declared_casting"]["by_table"]["t"]["by_column"]["age"]
    assert counts["sentinel_to_null"] == 1
    assert counts["parse_null"] == 1  # the empty field
    assert profile_descriptors(validate(cfg))["t"]["lexeme_columns"] == ["age"]
