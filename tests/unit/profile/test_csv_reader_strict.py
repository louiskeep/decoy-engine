"""CLI-DIALECT Part E fix round: strict-policy parser errors and the `nrows=0` read.

Under a strict (rooted) policy the file-access adapter turns every library failure into
`FileAccessReadError`. The headerless refusal has to survive that, so the adapter lets the
recognized pandas parser errors through for the reader to sanitize (and only those, and only on
the headerless path).
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

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
from decoy_engine.profile._csv_reader import CsvDialect, read_csv_source
from decoy_engine.validation_result import CODES

HEADERLESS = CsvDialect(False, ("a", "b", "c"), ",")
UNTERMINATED = b'zz_marker_a,zz_marker_b,"3\n'


def _rooted(root: Path) -> tuple[RootedFileAccess, int]:
    if not openat2_usable():
        pytest.skip("rooted file access needs Linux openat2")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    roots = [Root("main", fd, frozenset({FileRole.SOURCE}), frozenset({"read", "stat"}))]
    return RootedFileAccess(roots), fd


def _consume(result: Any) -> None:
    if not isinstance(result, pd.DataFrame):
        list(result)


@pytest.fixture
def strict(tmp_path: Path) -> Iterator[tuple[Path, RootedFileAccess]]:
    root = tmp_path / "root"
    root.mkdir()
    policy, fd = _rooted(root)
    try:
        yield root, policy
    finally:
        os.close(fd)


def _assert_value_free_refusal(err: BaseException) -> None:
    assert isinstance(err, PipelineValidationError)
    assert err.code == CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH
    assert str(err).startswith(f"{err.code}: source events ")
    for leak in ("zz_marker_a", "zz_marker_b", "events_secret.csv", "EOF inside", "tokenizing"):
        assert leak not in str(err) and leak not in repr(err)
    assert err.__cause__ is None and err.__context__ is None


@pytest.mark.parametrize("chunksize", [None, 2], ids=["whole", "chunked"])
def test_strict_policy_headerless_parser_disagreement_is_the_coded_refusal(
    strict: tuple[Path, RootedFileAccess], chunksize: int | None
) -> None:
    root, policy = strict
    (root / "events_secret.csv").write_bytes(UNTERMINATED)
    with pytest.raises(PipelineValidationError) as info:
        _consume(
            read_csv_source(
                "events_secret.csv",
                HEADERLESS,
                table="events",
                chunksize=chunksize,
                file_access=policy,
                field_key="k",
            )
        )
    _assert_value_free_refusal(info.value)


@pytest.mark.parametrize("chunksize", [None, 2], ids=["whole", "chunked"])
def test_strict_policy_never_carries_the_parser_text(
    strict: tuple[Path, RootedFileAccess], monkeypatch: pytest.MonkeyPatch, chunksize: int | None
) -> None:
    root, policy = strict
    (root / "events_secret.csv").write_bytes(b"1,2,3\n")

    def failing(*_a: Any, **_k: Any) -> pd.DataFrame:
        raise pd.errors.ParserError("zz_marker_a EOF inside string zz_marker_b")

    monkeypatch.setattr(pandas_adapter.pd, "read_csv", failing)
    with pytest.raises(PipelineValidationError) as info:
        _consume(
            read_csv_source(
                "events_secret.csv",
                HEADERLESS,
                table="events",
                chunksize=chunksize,
                file_access=policy,
                field_key="k",
            )
        )
    _assert_value_free_refusal(info.value)


@pytest.mark.parametrize("chunksize", [None, 2], ids=["whole", "chunked"])
def test_strict_policy_other_library_failures_stay_read_errors(
    strict: tuple[Path, RootedFileAccess], monkeypatch: pytest.MonkeyPatch, chunksize: int | None
) -> None:
    root, policy = strict
    (root / "events_secret.csv").write_bytes(b"1,2,3\n")

    def failing(*_a: Any, **_k: Any) -> pd.DataFrame:
        raise ValueError("zz_marker_a raw library text")

    monkeypatch.setattr(pandas_adapter.pd, "read_csv", failing)
    with pytest.raises(FileAccessReadError) as info:
        _consume(
            read_csv_source(
                "events_secret.csv",
                HEADERLESS,
                table="events",
                chunksize=chunksize,
                file_access=policy,
                field_key="k",
            )
        )
    assert "zz_marker_a" not in str(info.value) and "zz_marker_a" not in repr(info.value)


@pytest.mark.parametrize("chunksize", [None, 2], ids=["whole", "chunked"])
def test_strict_policy_header_path_parser_error_stays_a_read_error(
    strict: tuple[Path, RootedFileAccess], chunksize: int | None
) -> None:
    root, policy = strict
    (root / "h.csv").write_bytes(b'a,b\n1,"zz_marker_a\n')
    with pytest.raises(FileAccessReadError) as info:
        _consume(
            read_csv_source(
                "h.csv",
                CsvDialect(),
                table="events",
                chunksize=chunksize,
                file_access=policy,
                field_key="k",
            )
        )
    assert "zz_marker_a" not in str(info.value)


def test_the_plain_adapter_read_still_maps_a_parser_error(
    strict: tuple[Path, RootedFileAccess],
) -> None:
    """Other callers of `read_csv` get no pass-through: their parser text stays unreachable."""
    root, policy = strict
    (root / "bad.csv").write_bytes(UNTERMINATED)
    from decoy_engine.fileaccess import FileRequest

    request = FileRequest(FileRole.SOURCE, "read", "bad.csv", "k")
    with policy.open_read(request) as handle, pytest.raises(FileAccessReadError):
        pandas_adapter.read_csv(handle, header=None)


# nrows=0


def _oracle_zero(path: Path, dtype: type[str] | None) -> pd.DataFrame:
    """Plain pandas for a zero-row headerless read, with the declared names."""
    # Plain pandas on a clean stream: the empty frame does not depend on the file's content.
    frame = pd.read_csv(
        io.StringIO("1,2,3\n"), header=None, names=["a", "b", "c"], nrows=0, dtype=dtype
    )
    assert frame.shape == (0, 3)
    return frame


MALFORMED_FIRST = {
    "valid_first": b"1,2,3\n4,5,6\n",
    "short_first": b"1,2\n4,5,6\n",
    "long_first": b"1,2,3,4\n",
    "empty_file": b"",
    "bad_quote": b'zz,"unterminated\n',
    "nul_byte": b"1,2,\x003\n",
    "lone_cr": b"1,2,3\r4,5,6\r",
}


@pytest.mark.parametrize("dtype", [None, str], ids=["infer", "str"])
@pytest.mark.parametrize("raw", list(MALFORMED_FIRST.values()), ids=list(MALFORMED_FIRST))
def test_nrows_zero_returns_the_empty_frame_with_declared_columns(
    tmp_path: Path, raw: bytes, dtype: type[str] | None
) -> None:
    path = tmp_path / "z.csv"
    path.write_bytes(raw)
    got = read_csv_source(path, HEADERLESS, table="t", dtype=dtype, nrows=0)
    pd.testing.assert_frame_equal(got, _oracle_zero(path, dtype))
    assert list(got.columns) == ["a", "b", "c"]
    assert got.empty


@pytest.mark.parametrize("policy_kind", ["ambient", "strict"])
@pytest.mark.parametrize("chunked", [False, True], ids=["whole", "chunked"])
@pytest.mark.parametrize("raw", list(MALFORMED_FIRST.values()), ids=list(MALFORMED_FIRST))
def test_nrows_zero_succeeds_on_malformed_content_in_all_four_modes(
    tmp_path: Path,
    strict: tuple[Path, RootedFileAccess],
    raw: bytes,
    chunked: bool,
    policy_kind: str,
) -> None:
    root, policy = strict
    (root / "z.csv").write_bytes(raw)
    path = "z.csv" if policy_kind == "strict" else str(root / "z.csv")
    kwargs: dict[str, Any] = {"table": "t", "nrows": 0}
    if policy_kind == "strict":
        kwargs.update(file_access=policy, field_key="k")
    if chunked:
        kwargs["chunksize"] = 2
    got = read_csv_source(path, HEADERLESS, **kwargs)
    if chunked:
        assert list(got) == []  # exactly zero chunks, as pandas yields for nrows=0
    else:
        assert got.shape == (0, 3) and list(got.columns) == ["a", "b", "c"]


def test_nrows_zero_never_reaches_the_tokenizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation anchor: a zero-row read that falls through to pandas fails here."""
    path = tmp_path / "z.csv"
    path.write_bytes(b"1,2\n")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the tokenizer ran for nrows=0")

    for name in (
        "read_csv",
        "read_csv_chunks",
        "read_csv_parser_errors",
        "read_csv_chunks_parser_errors",
    ):
        monkeypatch.setattr(pandas_adapter, name, boom)
    assert read_csv_source(path, HEADERLESS, table="t", nrows=0).shape == (0, 3)
    assert list(read_csv_source(path, HEADERLESS, table="t", nrows=0, chunksize=2)) == []
    path.write_bytes(b"1,2,3\n")
    with pytest.raises(AssertionError):  # the guard itself is live for a real read
        read_csv_source(path, HEADERLESS, table="t", nrows=1)


@pytest.mark.parametrize("chunked", [False, True], ids=["whole", "chunked"])
def test_nrows_zero_still_fails_at_the_call_for_a_missing_file(
    tmp_path: Path, chunked: bool
) -> None:
    with pytest.raises(FileNotFoundError):
        read_csv_source(
            tmp_path / "absent.csv",
            HEADERLESS,
            table="t",
            nrows=0,
            chunksize=2 if chunked else None,
        )


def test_nrows_zero_is_still_policy_checked(strict: tuple[Path, RootedFileAccess]) -> None:
    root, policy = strict
    with pytest.raises(FileAccessDenied):
        read_csv_source(
            "../escape.csv", HEADERLESS, table="t", nrows=0, file_access=policy, field_key="k"
        )


def test_nrows_one_still_scans_the_first_record(tmp_path: Path) -> None:
    path = tmp_path / "z.csv"
    path.write_bytes(b"1,2\n")
    with pytest.raises(PipelineValidationError):
        read_csv_source(path, HEADERLESS, table="t", nrows=1)


def test_strict_policy_parser_error_on_a_later_chunk_step_is_the_coded_refusal(
    strict: tuple[Path, RootedFileAccess],
) -> None:
    """The width scan passes this file; pandas rejects the third record on its own step."""
    root, policy = strict
    (root / "events_secret.csv").write_bytes(b'1,2,3\n4,5,6\nzz_marker_a,zz_marker_b,"9\n')
    chunks = read_csv_source(
        "events_secret.csv",
        HEADERLESS,
        table="events",
        chunksize=1,
        file_access=policy,
        field_key="k",
    )
    assert len(next(chunks)) == 1
    with pytest.raises(PipelineValidationError) as info:
        list(chunks)
    _assert_value_free_refusal(info.value)
