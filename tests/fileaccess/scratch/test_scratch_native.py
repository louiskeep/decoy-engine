"""The scratch native boundary (F25 S1b): DuckDB and pyarrow failures leave value-free.

S1b holds the helper cells; each producer slice adds its own call-site cell here.
"""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import fileaccess
from decoy_engine.execution._isolated_common import is_memory_failure
from decoy_engine.fileaccess import (
    FileAccessDenied,
    FileAccessReadError,
    FileRole,
    Root,
    RootedFileAccess,
    ScratchArena,
    scratch_native,
)
from decoy_engine.fileaccess._scratch import scratch_native_iter
from tests.fileaccess.support import ALL_ACCESS

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the win32 arena is S1w")
SECRET = "/srv/secret-path"
SENTINELS = (SECRET, "patients_tbl", "ssn_col", "123-45-6789")


@pytest.fixture(params=["ambient", "strict"])
def policy(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[AbstractContextManager[object]]:
    if request.param == "ambient":
        yield nullcontext()
        return
    fd = os.open(tmp_path, os.O_RDONLY)
    roots = [Root("tmp", fd, frozenset(FileRole), ALL_ACCESS)]
    yield fileaccess.scope(RootedFileAccess(roots, allow_portable_fallback=True))
    os.close(fd)


def _copy_into_removed_dir(arena: ScratchArena) -> Callable[[], object]:
    conn = duckdb.connect()
    target = os.path.join(arena.local_path(), "patients_tbl", "ssn_col.parquet")
    return lambda: conn.execute(f"COPY (SELECT '123-45-6789' AS v) TO '{target}'")


def _cell_cast(arena: ScratchArena) -> Callable[[], object]:
    return lambda: duckdb.connect().execute("SELECT CAST('123-45-6789' AS INTEGER) AS ssn_col")


def _catalog(arena: ScratchArena) -> Callable[[], object]:
    return lambda: duckdb.connect().execute("SELECT ssn_col FROM patients_tbl")


def _arrow_cast(arena: ScratchArena) -> Callable[[], object]:
    return lambda: pa.array(["123-45-6789"]).cast(pa.int64())


def _arrow_read(arena: ScratchArena) -> Callable[[], object]:
    return lambda: pq.read_table(os.path.join(arena.local_path(), "patients_tbl.parquet"))


def _raw_os(arena: ScratchArena) -> Callable[[], object]:
    def fail() -> object:
        raise OSError(errno.EACCES, "Permission denied", f"{SECRET}/patients_tbl")

    return fail


# Each call fails inside a native library with a sentinel in its own text.
NATIVE = {
    "duckdb_copy_io": _copy_into_removed_dir,
    "duckdb_catalog": _catalog,
    "duckdb_conversion": _cell_cast,
    "arrow_cast": _arrow_cast,
    "arrow_read": _arrow_read,
    "os_error": _raw_os,
}


def _raw(call: Callable[[], object]) -> BaseException:
    with pytest.raises(Exception) as info:
        call()
    return info.value


def _assert_value_free(exc: BaseException) -> None:
    assert isinstance(exc, FileAccessReadError), repr(exc)
    assert exc.field_key == "engine.scratch"
    assert not any(s in f"{exc!s} {exc!r}" for s in SENTINELS), repr(exc)
    assert exc.__cause__ is None and exc.__context__ is None


@pytest.mark.parametrize("cell", sorted(NATIVE))
def test_native_failure_text_is_value_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: AbstractContextManager[object],
    cell: str,
) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    with ScratchArena.open() as arena, policy:
        call = NATIVE[cell](arena)
        raw = _raw(call)
        assert any(s in str(raw) for s in SENTINELS), f"non-vacuity: {raw!r}"
        with pytest.raises(FileAccessReadError) as info:
            scratch_native(call)
        _assert_value_free(info.value)


def test_batch_pulls_take_the_same_mapping(policy: AbstractContextManager[object]) -> None:
    def batches() -> Iterator[int]:
        yield 1
        pa.array(["123-45-6789"]).cast(pa.int64())
        yield 2  # pragma: no cover - the cast fails first

    pulled: list[int] = []
    with policy, pytest.raises(FileAccessReadError) as info:
        pulled.extend(scratch_native_iter(batches()))
    assert pulled == [1]
    _assert_value_free(info.value)
    assert list(scratch_native_iter(iter([3, 4]))) == [3, 4]


def _raise(exc: BaseException) -> Callable[[], object]:
    def fail() -> object:
        raise exc

    return fail


@pytest.mark.parametrize(
    "cell", ["memory_error", "duckdb_out_of_memory", "text_recognised_memory", "enospc", "denial"]
)
def test_classified_failures_keep_their_class(
    policy: AbstractContextManager[object], cell: str
) -> None:
    raised = {
        "memory_error": MemoryError(),
        "duckdb_out_of_memory": duckdb.OutOfMemoryException("out of memory"),
        "text_recognised_memory": pa.ArrowException("Unknown error: Wrapping 123-45-6789 failed"),
        "enospc": OSError(errno.ENOSPC, "No space left on device", f"{SECRET}/ssn_col"),
        "denial": FileAccessDenied("sources.patients"),
    }[cell]
    with policy, pytest.raises(BaseException) as info:
        scratch_native(_raise(raised))
    out = info.value
    if cell in ("memory_error", "duckdb_out_of_memory", "denial"):
        assert out is raised  # classified by type: the same object, untouched
        return
    assert type(out) is type(raised) and out is not raised
    assert not any(s in f"{out!s} {out!r}" for s in SENTINELS), repr(out)
    assert out.__cause__ is None and out.__context__ is None
    if cell == "enospc":
        assert isinstance(out, OSError) and out.errno == errno.ENOSPC and out.filename is None
    else:
        assert is_memory_failure(out)
