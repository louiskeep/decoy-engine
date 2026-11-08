"""CLI-DIALECT Part E: the one public CSV source reader (AT-E1 to AT-E7, AT-E9, AT-E10).

The oracle in every comparison is plain pandas called here with explicit
arguments, never the shared core, so a wrong core cannot vouch for itself.
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import decoy_engine
from decoy_engine import fileaccess
from decoy_engine._lexeme import LEXEME_KEY
from decoy_engine.errors import PipelineValidationError
from decoy_engine.fileaccess import (
    FileAccessDenied,
    FileAccessReadError,
    FileRole,
    Root,
    RootedFileAccess,
)
from decoy_engine.fileaccess._openat2 import openat2_usable
from decoy_engine.fileaccess.adapters import pandas as pandas_adapter
from decoy_engine.profile import _csv_reader
from decoy_engine.profile._csv_reader import CsvDialect, read_csv_source, resolve_csv_dialect
from decoy_engine.profile._readers import CsvFileSource, build_profile_source
from decoy_engine.profile._source import profile_source
from decoy_engine.validation_result import CODES

BASE_FRAMES = Path(__file__).parent / "fixtures" / "csv_reader_base_frames.json"


@dataclass(frozen=True)
class Case:
    name: str
    raw: bytes
    header: bool = True
    names: tuple[str, ...] = ()
    delim: str = ","


CASES = [
    Case("comma_header", b"id,name\n1,Ada\n2,Grace\n"),
    Case("tab_header", b"id\tname\n1\tAda\n2\tGrace\n", delim="\t"),
    Case("pipe_header", b"id|name\n1|Ada\n2|Grace\n", delim="|"),
    Case("headerless_comma", b"1,Ada\n2,Grace\n", False, ("id", "name")),
    Case("headerless_pipe", b"1|Ada\n2|Grace\n", False, ("id", "name"), "|"),
    Case("bom", b"\xef\xbb\xbfid,name\n1,Ada\n"),
    Case("headerless_bom", b"\xef\xbb\xbf1,Ada\n2,Grace\n", False, ("id", "name")),
    Case("crlf", b"id,name\r\n1,Ada\r\n2,Grace\r\n"),
    Case("quoted_delimiter", b'id,name\n1,"Lovelace, Ada"\n'),
    Case("quoted_newline", b'id,name\n1,"line1\nline2"\n2,x\n'),
    Case("blank_lines", b"id,name\n1,Ada\n\n2,Grace\n\n"),
    Case("headerless_blank_lines", b"1,Ada\n\n2,Grace\n\n", False, ("id", "name")),
    Case("no_trailing_newline", b"id,name\n1,Ada"),
    Case("one_column_headerless", b"x\ny\n", False, ("a",)),
]
CASE_IDS = [c.name for c in CASES]


def _write(tmp_path: Path, case: Case) -> Path:
    path = tmp_path / f"{case.name}.csv"
    path.write_bytes(case.raw)
    return path


def _dialect(case: Case) -> CsvDialect:
    return CsvDialect(case.header, case.names or None, case.delim)


def _oracle(path: Path, case: Case, dtype: type[str] | None) -> pd.DataFrame:
    if case.header:
        return pd.read_csv(path, sep=case.delim, dtype=dtype)
    return pd.read_csv(path, sep=case.delim, header=None, names=list(case.names), dtype=dtype)


def snapshot(frame: pd.DataFrame) -> dict[str, Any]:
    """The comparable form of a frame stored in the base-frames fixture."""
    return {
        "columns": [str(c) for c in frame.columns],
        "dtypes": [str(t) for t in frame.dtypes],
        "json": frame.to_json(orient="split", date_format="iso"),
    }


def _file_source(path: Path, case: Case) -> CsvFileSource:
    return CsvFileSource(
        path,
        has_header=case.header,
        column_names=list(case.names) if case.names else None,
        delimiter=case.delim,
    )


def _rooted(root: Path) -> tuple[RootedFileAccess, int]:
    if not openat2_usable():
        pytest.skip("rooted file access needs Linux openat2")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    roots = [Root("main", fd, frozenset({FileRole.SOURCE}), frozenset({"read", "stat"}))]
    return RootedFileAccess(roots), fd


# AT-E1


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
@pytest.mark.parametrize("dtype", [None, str], ids=["infer", "str"])
def test_reader_matches_plain_pandas(tmp_path: Path, case: Case, dtype: type[str] | None) -> None:
    path = _write(tmp_path, case)
    got = read_csv_source(path, _dialect(case), table="t", dtype=dtype)
    pd.testing.assert_frame_equal(got, _oracle(path, case, dtype))


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_profile_source_matches_plain_pandas_and_base(tmp_path: Path, case: Case) -> None:
    path = _write(tmp_path, case)
    got = _file_source(path, case).to_frame()
    pd.testing.assert_frame_equal(got, _oracle(path, case, None))
    base = json.loads(BASE_FRAMES.read_text(encoding="utf-8"))
    assert snapshot(got) == base[case.name]


# AT-E2


def test_default_dialect_is_plain_read_csv(tmp_path: Path) -> None:
    raw = b'\xef\xbb\xbfid,note,flag\n001,,NA\n2,"a, b",null\n3,"",N/A\n'
    path = tmp_path / "d.csv"
    path.write_bytes(raw)
    got = read_csv_source(path, CsvDialect(), table="t", dtype=str)
    pd.testing.assert_frame_equal(got, pd.read_csv(path, dtype=str))
    assert got["flag"].isna().all()  # NA strings are NaN, exactly as plain pandas


# AT-E3 and AT-E4

_MISFITS = {
    "four_fields": b"zz_marker_a,zz_marker_b,3\n1,2,3,zz_marker_b\n",
    "two_fields": b"zz_marker_a,zz_marker_b,3\nzz_marker_a,zz_marker_b\n",
    "blank_only": b"\n\n",
    "nul": b"zz_marker_a,zz_marker_b,3\x00\n",
    "lone_cr": b"zz_marker_a,zz_marker_b,3\r4,5,6\n",
    "unterminated_quote": b'zz_marker_a,zz_marker_b,"3\n',
}
_CODE = {
    "four_fields": CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
    "two_fields": CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
    "blank_only": CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
    "nul": CODES.SOURCE_FILE_NUL_BYTE,
    "lone_cr": CODES.SOURCE_FILE_LONE_CR,
    "unterminated_quote": CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH,
}
# The unterminated quote passes the width scan; pandas rejects it while reading, so a chunked
# read raises it on the first step, not at the call.
_SCAN_CAUGHT = [k for k in _MISFITS if k != "unterminated_quote"]
_HEADERLESS = CsvDialect(False, ("a", "b", "c"), ",")


def _consume(result: Any) -> None:
    if not isinstance(result, pd.DataFrame):
        list(result)


@pytest.mark.parametrize("chunksize", [None, 2], ids=["whole", "chunked"])
@pytest.mark.parametrize("kind", list(_MISFITS))
def test_headerless_refusals_are_coded_and_value_free(
    tmp_path: Path, kind: str, chunksize: int | None
) -> None:
    path = tmp_path / "events_secret_name.csv"
    path.write_bytes(_MISFITS[kind])
    with pytest.raises(PipelineValidationError) as info:
        _consume(read_csv_source(path, _HEADERLESS, table="events", chunksize=chunksize))
    err = info.value
    assert err.code == _CODE[kind]
    assert str(err).startswith(f"{err.code}: source events ")
    for leak in ("zz_marker_a", "zz_marker_b", path.name, str(tmp_path)):
        assert leak not in str(err) and leak not in repr(err)
    assert err.__cause__ is None and err.__context__ is None


def test_misfit_row_message_names_line_and_counts(tmp_path: Path) -> None:
    path = tmp_path / "x.csv"
    path.write_bytes(_MISFITS["four_fields"])
    with pytest.raises(PipelineValidationError) as info:
        read_csv_source(path, _HEADERLESS, table="events")
    assert str(info.value).endswith("line 2 has 4 fields but column_names declares 3")


@pytest.mark.parametrize("kind", _SCAN_CAUGHT)
def test_chunked_scan_is_eager(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "x.csv"
    path.write_bytes(_MISFITS[kind])
    with pytest.raises(PipelineValidationError):
        read_csv_source(path, _HEADERLESS, table="events", chunksize=2)  # no next()


def test_chunked_scan_sees_a_misfit_past_the_first_chunk(tmp_path: Path) -> None:
    path = tmp_path / "x.csv"
    path.write_bytes(b"1,2,3\n4,5,6\n7,8,9\n1,2\n")
    with pytest.raises(PipelineValidationError, match="line 4 has 2 fields"):
        read_csv_source(path, _HEADERLESS, table="events", chunksize=2)


def test_chunks_concatenate_to_the_whole_read(tmp_path: Path) -> None:
    path = tmp_path / "x.csv"
    path.write_bytes(b"1,2,3\n4,5,6\n7,8,9\n10,11,12\n13,14,15\n")
    whole = read_csv_source(path, _HEADERLESS, table="t", dtype=str)
    chunks = list(read_csv_source(path, _HEADERLESS, table="t", dtype=str, chunksize=2))
    assert [len(c) for c in chunks] == [2, 2, 1]
    pd.testing.assert_frame_equal(pd.concat(chunks, ignore_index=True), whole)
    header = tmp_path / "h.csv"
    header.write_bytes(b"a,b\n1,2\n3,4\n5,6\n")
    hchunks = read_csv_source(header, CsvDialect(), table="t", dtype=str, chunksize=2)
    pd.testing.assert_frame_equal(
        pd.concat(list(hchunks), ignore_index=True), pd.read_csv(header, dtype=str)
    )


def test_a_missing_file_fails_at_the_call_even_when_chunked(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_csv_source(tmp_path / "absent.csv", CsvDialect(), table="t", chunksize=2)


def test_nrows_scans_only_the_rows_it_consumes(tmp_path: Path) -> None:
    path = tmp_path / "x.csv"
    path.write_bytes(b"1,2,3\n4,5,6\n1,2\n")
    got = read_csv_source(path, _HEADERLESS, table="t", nrows=2)
    assert len(got) == 2
    with pytest.raises(PipelineValidationError):
        read_csv_source(path, _HEADERLESS, table="t")


def test_dtype_other_than_none_or_str_is_a_type_error(tmp_path: Path) -> None:
    path = tmp_path / "x.csv"
    path.write_bytes(b"a\n1\n")
    with pytest.raises(TypeError):
        read_csv_source(path, CsvDialect(), table="t", dtype=int)  # type: ignore[call-overload]


# AT-E5


def test_resolve_csv_dialect_defaults_and_explicit_keys() -> None:
    from decoy_engine.config import FileSource

    plain = FileSource.model_validate({"type": "file", "format": "csv", "path": "a.csv"})
    assert resolve_csv_dialect(plain.model_dump()) == CsvDialect(
        plain.has_header, plain.column_names, plain.delimiter
    )
    descriptor = {
        "type": "file",
        "format": "csv",
        "path": "a.csv",
        "has_header": False,
        "column_names": ["x", "y"],
        "delimiter": "\t",
    }
    assert resolve_csv_dialect(descriptor) == CsvDialect(False, ("x", "y"), "\t")


def _headerless_config(path: Path, table: str, delimiter: str = ",") -> dict[str, Any]:
    source = {
        "type": "file",
        "format": "csv",
        "path": str(path),
        "has_header": False,
        "column_names": ["id", "name"],
    }
    if delimiter != ",":
        source["delimiter"] = delimiter
    return {"sources": {table: source}}


def test_profile_source_resolves_the_dialect_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.profile import _readers

    path = tmp_path / "t.tsv"
    path.write_bytes(b"1\tAda\n2\tGrace\n")
    calls: list[dict[str, Any]] = []
    real = _readers.resolve_csv_dialect

    def spy(descriptor: Any) -> CsvDialect:
        calls.append(dict(descriptor))
        return real(descriptor)

    monkeypatch.setattr(_readers, "resolve_csv_dialect", spy)
    profile = profile_source(_headerless_config(path, "events", "\t"), seed=1)
    assert len(calls) == 1
    assert [c.name for c in profile.tables[0].columns] == ["id", "name"]


# AT-E6


def test_public_surface_and_policy(tmp_path: Path) -> None:
    for name, obj in (
        ("CsvDialect", CsvDialect),
        ("read_csv_source", read_csv_source),
        ("resolve_csv_dialect", resolve_csv_dialect),
    ):
        assert name in decoy_engine.__all__
        assert getattr(decoy_engine, name) is obj
        assert getattr(_csv_reader, name) is obj
    assert "decoy_engine.profile._csv_reader.read_csv_source" in fileaccess.ENTRY_POINTS

    outside = tmp_path / "outside"
    outside.mkdir()
    allowed = tmp_path / "root"
    allowed.mkdir()
    (outside / "a.csv").write_bytes(b"a,b\n1,2\n")
    (allowed / "a.csv").write_bytes(b"a,b\n1,2\n3,4\n5,6\n")
    policy, fd = _rooted(allowed)
    try:
        with pytest.raises(FileAccessDenied):
            read_csv_source(
                outside / "a.csv", CsvDialect(), table="t", file_access=policy, field_key="k"
            )
        chunks = read_csv_source(
            "a.csv", CsvDialect(), table="t", chunksize=2, file_access=policy, field_key="k"
        )
        # The policy scope of the call is over; the chunks still read under the captured one.
        assert [len(c) for c in chunks] == [2, 1]
    finally:
        os.close(fd)


# AT-E7


def test_profile_refusal_names_table_not_file(tmp_path: Path) -> None:
    path = tmp_path / "customers_file_name.csv"
    path.write_bytes(b"1,Ada\n2,Grace,extra\n")
    with pytest.raises(PipelineValidationError) as info:
        profile_source(_headerless_config(path, "customers"), seed=1)
    text = str(info.value)
    assert text.startswith("source_file.column_count_mismatch: source customers line ")
    assert path.name not in text


# AT-E9


def test_headerless_lexeme_columns_keep_text(tmp_path: Path) -> None:
    path = tmp_path / "lex.csv"
    path.write_bytes(b"1,007\n2,010\n")
    oracle = pd.read_csv(path, header=None, names=["a", "b"], converters={1: str})
    assert oracle["b"].tolist() == ["007", "010"]
    source = CsvFileSource(path, has_header=False, column_names=["a", "b"], lexeme_columns=("b",))
    pd.testing.assert_frame_equal(source.to_frame(), oracle)

    descriptor = {
        "type": "file",
        "format": "csv",
        "path": str(path),
        "has_header": False,
        "column_names": ["a", "b"],
        LEXEME_KEY: ["b"],
    }
    assert build_profile_source(descriptor).to_frame()["b"].tolist() == ["007", "010"]

    with_header = tmp_path / "lex_h.csv"
    with_header.write_bytes(b"a,b\n1,007\n")
    assert CsvFileSource(with_header, lexeme_columns=("b",)).to_frame()["b"].tolist() == ["007"]


# AT-E10


def test_read_csv_chunks_is_guarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = tmp_path / "root"
    allowed.mkdir()
    (allowed / "a.csv").write_bytes(b"a\n1\n2\n3\n")

    def failing(*_a: Any, **_k: Any) -> Iterator[pd.DataFrame]:
        yield pd.DataFrame({"a": ["1"]})
        raise ValueError("zz_marker_a raw library text")

    monkeypatch.setattr(pandas_adapter.pd, "read_csv", failing)
    policy, fd = _rooted(allowed)
    try:
        chunks = read_csv_source(
            "a.csv", CsvDialect(), table="t", chunksize=1, file_access=policy, field_key="k"
        )
        assert len(next(chunks)) == 1
        with pytest.raises(FileAccessReadError) as info:
            next(chunks)
        assert "zz_marker_a" not in str(info.value)
    finally:
        os.close(fd)


def test_signature_keyword_only_tail() -> None:
    params = inspect.signature(read_csv_source).parameters
    assert [n for n, p in params.items() if p.kind is p.KEYWORD_ONLY] == [
        "table",
        "dtype",
        "nrows",
        "chunksize",
        "file_access",
        "field_key",
        "columns",
    ]


# -- column projection ----------------------------------------------------------------------


def _projected(tmp_path: Any, text: str, dialect: CsvDialect, **kw: Any) -> Any:
    path = tmp_path / "p.csv"
    path.write_text(text)
    return read_csv_source(path, dialect, table="t", dtype=str, **kw)


_HEADERLESS = CsvDialect(has_header=False, column_names=["a", "b", "c"])


@pytest.mark.parametrize(
    ("text", "dialect"),
    [("a,b,c\n1,2,3\n4,5,6\n", CsvDialect()), ("1,2,3\n4,5,6\n", _HEADERLESS)],
)
def test_projection_returns_exactly_the_requested_order(
    tmp_path: Any, text: str, dialect: CsvDialect
) -> None:
    whole = _projected(tmp_path, text, dialect, columns=["c", "a"])
    assert list(whole.columns) == ["c", "a"]
    assert whole.to_dict("list") == {"c": ["3", "6"], "a": ["1", "4"]}
    chunks = list(_projected(tmp_path, text, dialect, columns=["c", "a"], chunksize=1))
    assert [list(c.columns) for c in chunks] == [["c", "a"], ["c", "a"]]
    empty = _projected(tmp_path, text, dialect, columns=["c", "a"], nrows=0)
    assert list(empty.columns) == ["c", "a"] and len(empty) == 0


@pytest.mark.parametrize(
    ("text", "dialect"),
    [("a,b,c\n1,2,3\n", CsvDialect()), ("1,2,3\n", _HEADERLESS)],
)
def test_projection_refuses_unknown_and_repeated_names_without_quoting_them(
    tmp_path: Any, text: str, dialect: CsvDialect
) -> None:
    for bad in (["zz_secret_name"], ["a", "a"], []):
        with pytest.raises(ValueError) as info:
            _projected(tmp_path, text, dialect, columns=bad)
        assert "zz_secret_name" not in str(info.value)
