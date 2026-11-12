"""CONFIG-EXPLICIT E1: a headerless csv source can name its columns in a one-record file.

AT-E1-4 (the header file reads like a data file's header row), AT-E1-5 (every refusal is coded
and value-free), AT-E1-6 (profiling reads the same as inline names) and AT-E1-10 (the resolver
and `build_profile_source` are file-access entry points).
"""

from __future__ import annotations

import errno
import inspect
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import fileaccess
from decoy_engine._lexeme import LEXEME_KEY
from decoy_engine.errors import PipelineValidationError
from decoy_engine.fileaccess import (
    FileAccessDenied,
    FileAccessUnavailable,
    FileRole,
    LocalFileAccess,
    Root,
    RootedFileAccess,
)
from decoy_engine.fileaccess._openat2 import openat2_usable
from decoy_engine.profile import _csv_reader
from decoy_engine.profile._csv_reader import CsvDialect, read_csv_source, resolve_csv_dialect
from decoy_engine.profile._readers import build_profile_source
from decoy_engine.profile._source import profile_source
from decoy_engine.validation_result import CODES
from tests._declared_schema import declare_rest

NAMES_FILE = "zz_names_file.csv"


def _source(path: Path | str, **extra: Any) -> dict[str, Any]:
    return {"type": "file", "format": "csv", "path": str(path), "has_header": False, **extra}


@pytest.fixture
def rooted(tmp_path: Path) -> Iterator[tuple[RootedFileAccess, Path]]:
    if not openat2_usable():
        pytest.skip("rooted file access needs Linux openat2")
    root = tmp_path / "root"
    root.mkdir()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    roots = [Root("main", fd, frozenset({FileRole.SOURCE}), frozenset({"read", "stat"}))]
    try:
        yield RootedFileAccess(roots), root
    finally:
        os.close(fd)


# AT-E1-4

HEADER_CASES = {
    "comma": (b"a,b,c\n", ",", ["a", "b", "c"]),
    "tab": (b"a\tb\n", "\t", ["a", "b"]),
    "pipe": (b"a|b\n", "|", ["a", "b"]),
    "bom": (b"\xef\xbb\xbfa,b\n", ",", ["a", "b"]),
    "quoted-delimiter": (b'"a,x",b\n', ",", ["a,x", "b"]),
    "crlf": (b"a,b\r\n", ",", ["a", "b"]),
    "blank-lines": (b"\n\na,b\n\n", ",", ["a", "b"]),
    "whitespace-line": (b"  \na,b\n", ",", ["a", "b"]),
    "spaces": (b" a , b\n", ",", [" a ", " b"]),
}


@pytest.mark.parametrize("case", sorted(HEADER_CASES))
def test_header_file_resolves(tmp_path: Path, case: str) -> None:
    content, delimiter, names = HEADER_CASES[case]
    header = tmp_path / NAMES_FILE
    header.write_bytes(content)
    resolved = resolve_csv_dialect(
        _source("d.csv", column_names_file=str(header), delimiter=delimiter), table="events"
    )
    inline = resolve_csv_dialect(_source("d.csv", column_names=names, delimiter=delimiter))
    assert resolved == inline
    assert resolved.column_names == tuple(names)
    as_data_header = read_csv_source(
        header, CsvDialect(delimiter=delimiter), table="events", dtype=str
    )
    assert list(as_data_header.columns) == names


@pytest.mark.parametrize(
    ("record", "blank"),
    [([], True), ([""], True), (["  "], True), (["a"], False), (["", ""], False)],
)
def test_one_blank_record_predicate(record: list[str], blank: bool) -> None:
    assert _csv_reader._is_blank_record(record) is blank


# AT-E1-5

REFUSALS: dict[str, bytes | None] = {
    "not found": None,
    "not UTF-8 text": b"zz_marker_b,\xff\xfe\n",
    "empty": b"\n  \n",
    "more than one record": b"zz_marker_b,c\nzz_marker_a,d\n",
    "too large": b"zz_marker_b," + b"x" * (1 << 20) + b"\n",
    "NUL byte": b"zz_marker_b\x00,c\n",
    "lone carriage return": b"zz_marker_b\rc\n",
    "blank name at position 2": b"zz_marker_b,,c\n",
    "duplicate name at positions 1 and 3": b"zz_marker_b,c,zz_marker_b\n",
}


def _refusal(tmp_path: Path, locator: str) -> PipelineValidationError:
    with pytest.raises(PipelineValidationError) as info:
        resolve_csv_dialect(_source("d.csv", column_names_file=locator), table="events")
    return info.value


def _assert_value_free(err: PipelineValidationError, phrase: str, tmp_path: Path) -> None:
    assert err.code == CODES.SOURCE_FILE_COLUMN_NAMES_FILE_INVALID
    assert str(err).startswith("source_file.column_names_file_invalid: source events ")
    assert str(err).endswith(f"({phrase})")
    for text in (str(err), repr(err), repr(err.args)):
        for marker in ("zz_marker_a", "zz_marker_b", "zz_names_file", str(tmp_path)):
            assert marker not in text
    assert err.__cause__ is None and err.__context__ is None


@pytest.mark.parametrize("phrase", sorted(REFUSALS))
def test_header_file_refusals_are_value_free(tmp_path: Path, phrase: str) -> None:
    header = tmp_path / NAMES_FILE
    content = REFUSALS[phrase]
    if content is not None:
        header.write_bytes(content)
    _assert_value_free(_refusal(tmp_path, str(header)), phrase, tmp_path)


@pytest.mark.parametrize("locator", ["/dev/null", "dir"], ids=["char-device", "directory"])
def test_a_non_regular_header_file_is_not_readable(tmp_path: Path, locator: str) -> None:
    if locator == "dir":
        (tmp_path / NAMES_FILE).mkdir()
        locator = str(tmp_path / NAMES_FILE)
    _assert_value_free(_refusal(tmp_path, locator), "not readable", tmp_path)


# AT-E1-6


def _columns(profile: Any) -> list[tuple[Any, ...]]:
    return [
        (c.name, c.row_count, c.null_count, c.distinct_count) for c in profile.tables[0].columns
    ]


def test_profile_reads_header_file_like_inline_names(tmp_path: Path) -> None:
    data = tmp_path / "events.csv"
    data.write_bytes(b"1,Ada,007\n2,,010\n3,Ada,007\n")
    header = tmp_path / NAMES_FILE
    header.write_bytes(b"id,name,code\n")
    names = ["id", "name", "code"]
    by_file = _source(data, column_names_file=str(header))
    by_list = _source(data, column_names=names)
    from_file = profile_source(declare_rest({"sources": {"events": by_file}}), seed=1)
    from_list = profile_source(declare_rest({"sources": {"events": by_list}}), seed=1)
    assert _columns(from_file) == _columns(from_list)
    assert [c[0] for c in _columns(from_file)] == names

    frames = [
        read_csv_source(data, resolve_csv_dialect(d), table="events", dtype=str)
        for d in (by_file, by_list)
    ]
    assert frames[0].equals(frames[1])

    lexeme = build_profile_source({**by_file, LEXEME_KEY: ["code"]}).to_frame()
    assert lexeme["code"].tolist() == ["007", "010", "007"]


# AT-E1-10

ENTRIES = (
    ("decoy_engine.profile._csv_reader.resolve_csv_dialect", resolve_csv_dialect),
    ("decoy_engine.profile._readers.build_profile_source", build_profile_source),
)


def test_csv_dialect_policy_entry_points() -> None:
    assert list(fileaccess.ENTRY_POINTS) == sorted(set(fileaccess.ENTRY_POINTS))
    for dotted, fn in ENTRIES:
        assert dotted in fileaccess.ENTRY_POINTS
        assert getattr(fn, "__decoy_entry_point__", False) is True
        param = inspect.signature(fn).parameters["file_access"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is None


def test_a_rooted_policy_denies_a_header_file_outside_its_root(
    tmp_path: Path, rooted: tuple[RootedFileAccess, Path]
) -> None:
    policy, root = rooted
    (root / "d.csv").write_bytes(b"1,2\n")
    outside = tmp_path / NAMES_FILE
    outside.write_bytes(b"a,b\n")
    descriptor = _source("d.csv", column_names_file=str(outside))
    with pytest.raises(FileAccessDenied):
        resolve_csv_dialect(descriptor, table="events", file_access=policy)
    with pytest.raises(FileAccessDenied):
        profile_source({"sources": {"events": descriptor}}, seed=1, file_access=policy)
    with pytest.raises(FileAccessDenied):
        build_profile_source(descriptor, table="events", file_access=policy)
    (root / NAMES_FILE).write_bytes(b"a,b\n")
    inside = _source("d.csv", column_names_file=NAMES_FILE)
    assert resolve_csv_dialect(inside, file_access=policy).column_names == ("a", "b")


def test_an_explicit_policy_never_widens_the_installed_one(
    rooted: tuple[RootedFileAccess, Path],
) -> None:
    policy, root = rooted
    (root / NAMES_FILE).write_bytes(b"a,b\n")

    @fileaccess.entry_point
    def outer(*, file_access: Any = None) -> object:
        return resolve_csv_dialect(
            _source("d.csv", column_names_file=NAMES_FILE), file_access=LocalFileAccess()
        )

    with pytest.raises(FileAccessUnavailable):
        outer(file_access=policy)


def test_no_policy_under_forbid_ambient_refuses_even_inline_names() -> None:
    code = textwrap.dedent(
        """
        from decoy_engine import fileaccess
        from decoy_engine.fileaccess import FileAccessUnavailable
        from decoy_engine.profile._csv_reader import resolve_csv_dialect

        fileaccess.forbid_ambient()
        try:
            resolve_csv_dialect({"has_header": False, "column_names": ["a"]})
        except FileAccessUnavailable:
            print("refused")
        """
    )
    out = subprocess.run(  # noqa: S603 - runs this interpreter on a fixed script
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert out.stdout.strip() == "refused"


def test_a_resource_errno_survives_a_strict_policy(
    monkeypatch: pytest.MonkeyPatch, rooted: tuple[RootedFileAccess, Path]
) -> None:
    policy, root = rooted
    (root / NAMES_FILE).write_bytes(b"a,b\n")

    def full_disk(*args: Any, **kwargs: Any) -> bytes:
        raise OSError(errno.ENOSPC, "zz_marker_a")

    monkeypatch.setattr(fileaccess, "read_checked", full_disk)
    with pytest.raises(OSError) as info:
        resolve_csv_dialect(_source("d.csv", column_names_file=NAMES_FILE), file_access=policy)
    assert info.value.errno == errno.ENOSPC
    assert not isinstance(info.value, PipelineValidationError)


def test_only_an_os_error_becomes_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    header = tmp_path / NAMES_FILE
    header.write_bytes(b"a,b\n")

    def broken(*args: Any, **kwargs: Any) -> bytes:
        raise RuntimeError("zz_marker_a")

    monkeypatch.setattr(fileaccess, "read_checked", broken)
    with pytest.raises(RuntimeError):
        resolve_csv_dialect(_source("d.csv", column_names_file=str(header)), table="events")
