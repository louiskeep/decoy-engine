"""CLI-DIALECT Part E fix round: strict-policy parser errors and the `nrows=0` read.

Under a strict (rooted) policy the file-access adapter turns every library failure into
`FileAccessReadError`. The headerless refusal has to survive that, so the adapter lets the
recognized pandas parser errors through for the reader to sanitize (and only those, and only on
the headerless path).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from decoy_engine.errors import PipelineValidationError
from decoy_engine.fileaccess import (
    FileAccessReadError,
    FileRole,
    Root,
    RootedFileAccess,
)
from decoy_engine.fileaccess.adapters import pandas as pandas_adapter
from decoy_engine.profile._csv_reader import CsvDialect, read_csv_source
from decoy_engine.validation_result import CODES

HEADERLESS = CsvDialect(False, ("a", "b", "c"), ",")
UNTERMINATED = b'zz_marker_a,zz_marker_b,"3\n'


def _rooted(root: Path) -> tuple[RootedFileAccess, int]:
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
    frame = pd.read_csv(path, header=None, names=["a", "b", "c"], nrows=0, dtype=dtype)
    assert frame.shape == (0, 3)
    return frame


@pytest.mark.parametrize("dtype", [None, str], ids=["infer", "str"])
@pytest.mark.parametrize(
    "raw",
    [b"1,2,3\n4,5,6\n", b"1,2\n4,5,6\n", b"1,2,3,4\n", b"", b'zz,"unterminated\n'],
    ids=["valid_first", "short_first", "long_first", "empty_file", "bad_quote"],
)
def test_nrows_zero_returns_the_empty_frame_with_declared_columns(
    tmp_path: Path, raw: bytes, dtype: type[str] | None
) -> None:
    path = tmp_path / "z.csv"
    path.write_bytes(raw)
    if raw.startswith(b"zz,"):
        # pandas itself cannot tokenize this one; the reader keeps its coded refusal.
        with pytest.raises(PipelineValidationError):
            read_csv_source(path, HEADERLESS, table="t", dtype=dtype, nrows=0)
        return
    got = read_csv_source(path, HEADERLESS, table="t", dtype=dtype, nrows=0)
    pd.testing.assert_frame_equal(got, _oracle_zero(path, dtype))
    assert list(got.columns) == ["a", "b", "c"]
    assert got.empty


def test_nrows_zero_does_not_scan_the_first_record(tmp_path: Path) -> None:
    path = tmp_path / "z.csv"
    path.write_bytes(b"1,2\n")  # malformed first row
    assert read_csv_source(path, HEADERLESS, table="t", nrows=0).shape == (0, 3)
    with pytest.raises(PipelineValidationError):
        read_csv_source(path, HEADERLESS, table="t", nrows=1)


def test_nrows_zero_chunked_yields_no_rows(tmp_path: Path) -> None:
    path = tmp_path / "z.csv"
    path.write_bytes(b"1,2\n")
    chunks = list(read_csv_source(path, HEADERLESS, table="t", nrows=0, chunksize=2))
    assert sum(len(c) for c in chunks) == 0
    assert all(list(c.columns) == ["a", "b", "c"] for c in chunks)


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
