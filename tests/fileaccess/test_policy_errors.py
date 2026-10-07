"""A strict policy denies only what the policy decides. A library failure on an authorized
file is a typed read error carrying the config position and nothing else, and memory and
disk-resource failures keep their class so their classifiers still work."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.fileaccess import (
    FileAccessDenied,
    FileAccessReadError,
    FileRequest,
    FileRole,
    RootedFileAccess,
)
from decoy_engine.fileaccess._errors import guarded, guarded_iter
from decoy_engine.fileaccess.adapters import arrow, serial


def _handle(rooted: RootedFileAccess, name: str):  # type: ignore[no-untyped-def]
    return rooted.open_read(FileRequest(FileRole.REFERENCE, "read", name, "joint_mask.reference"))


def _assert_clean(exc: BaseException, root_dir: Path) -> None:
    text = f"{exc!s} {exc!r}"
    assert str(root_dir) not in text and "/proc/self/fd" not in text
    assert exc.__cause__ is None and exc.__suppress_context__ and exc.__context__ is None


def test_corrupt_parquet_is_a_read_error_not_a_denial(rooted, root_dir) -> None:  # type: ignore[no-untyped-def]
    (root_dir / "bad.parquet").write_bytes(b"PAR1 not really parquet at all")
    with pytest.raises(FileAccessReadError) as ei:
        arrow.read_table(_handle(rooted, "bad.parquet"))
    assert not isinstance(ei.value, FileAccessDenied)
    assert str(ei.value) == "file could not be read: joint_mask.reference"
    _assert_clean(ei.value, root_dir)


def test_malformed_yaml_and_json_are_read_errors(rooted, root_dir) -> None:  # type: ignore[no-untyped-def]
    (root_dir / "bad.yaml").write_text("a: [unclosed\n  - : :\n")
    (root_dir / "bad.json").write_text("{nope")
    with pytest.raises(FileAccessReadError) as ei:
        serial.yaml_safe_load(_handle(rooted, "bad.yaml"))
    _assert_clean(ei.value, root_dir)
    with pytest.raises(FileAccessReadError) as ej:
        serial.json_load(_handle(rooted, "bad.json"))
    _assert_clean(ej.value, root_dir)


def test_an_undecodable_text_file_is_a_read_error_with_no_byte_in_it(rooted, root_dir) -> None:  # type: ignore[no-untyped-def]
    (root_dir / "latin.txt").write_bytes(b"secret\xff\xfebytes")
    with pytest.raises(FileAccessReadError) as ei:
        serial.read_text(_handle(rooted, "latin.txt"))
    _assert_clean(ei.value, root_dir)
    assert "xff" not in str(ei.value).lower() and "codec" not in str(ei.value)


@pytest.mark.parametrize(
    "exc",
    [MemoryError("out"), pa.ArrowMemoryError("malloc of size 9 failed")],
)
def test_memory_errors_keep_their_class(exc: BaseException) -> None:
    def boom() -> None:
        raise exc

    with pytest.raises(type(exc)):
        guarded("k", True, boom)
    with pytest.raises(type(exc)):
        list(guarded_iter("k", True, _raising_iter(exc)))


def _raising_iter(exc: BaseException):  # type: ignore[no-untyped-def]
    yield 1
    raise exc


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT])
def test_disk_resource_errors_keep_their_class_without_the_filename(code: int) -> None:
    def full() -> None:
        raise OSError(code, os.strerror(code), "/tmp/secret/stage-name")

    with pytest.raises(OSError) as ei:
        guarded("k", True, full)
    assert ei.value.errno == code
    assert ei.value.filename is None and "secret" not in str(ei.value)
    assert ei.value.__cause__ is None


def test_the_engine_oom_classifier_still_recognises_a_strict_failure() -> None:
    from decoy_engine.execution._isolated_common import is_memory_failure

    def boom() -> None:
        raise MemoryError

    with pytest.raises(MemoryError) as ei:
        guarded("k", True, boom)
    assert is_memory_failure(ei.value)


def test_a_denial_from_inside_passes_unchanged_and_stop_iteration_ends_a_loop() -> None:
    def deny_it() -> None:
        raise FileAccessDenied("x")

    with pytest.raises(FileAccessDenied):
        guarded("k", True, deny_it)
    assert list(guarded_iter("k", True, iter([1, 2]))) == [1, 2]


def test_a_library_error_inside_an_iterator_step_is_the_read_error() -> None:
    def gen():  # type: ignore[no-untyped-def]
        yield 1
        raise ValueError("/secret/path/inside/library text")

    got = []
    with pytest.raises(FileAccessReadError) as ei:
        for item in guarded_iter("a.b", True, gen()):
            got.append(item)
    assert got == [1] and "secret" not in str(ei.value)


def test_the_local_policy_leaves_library_errors_alone() -> None:
    def boom() -> None:
        raise ValueError("library text")

    with pytest.raises(ValueError, match="library text"):
        guarded("k", False, boom)


def test_a_non_utf8_secret_file_under_a_strict_policy_leaks_nothing(rooted, root_dir) -> None:  # type: ignore[no-untyped-def]
    from decoy_engine.keyprovider import MaskSecretError, resolve_mask_secret_ref

    (root_dir / "secret.bin").write_bytes(b"abcdef\xfe\xfdSECRETTAIL" + b"z" * 40)
    with pytest.raises(MaskSecretError) as ei:
        resolve_mask_secret_ref("file:secret.bin", file_access=rooted)
    exc = ei.value
    text = f"{exc!s} {exc!r}"
    assert "secret.bin" not in text and "SECRETTAIL" not in text and "0xfe" not in text
    assert "global_settings.mask_secret_ref" in text
    assert exc.__cause__ is None and exc.__context__ is None


def test_an_unreadable_secret_under_a_strict_policy_is_a_denial_not_a_path(rooted) -> None:  # type: ignore[no-untyped-def]
    from decoy_engine.keyprovider import resolve_mask_secret_ref

    with pytest.raises(FileAccessDenied) as ei:
        resolve_mask_secret_ref("file:missing.txt", file_access=rooted)
    assert "missing.txt" not in str(ei.value)


def _memory_shapes():  # type: ignore[no-untyped-def]
    import duckdb

    return [
        MemoryError("x"),
        pa.ArrowMemoryError("malloc failed"),
        duckdb.OutOfMemoryException("Out of Memory Error: could not allocate block"),
        ValueError("not able to copy ctx"),
        pa.ArrowInvalid("Unknown error: Wrapping 5 failed"),
        OSError(errno.ENOMEM, os.strerror(errno.ENOMEM)),
    ]


@pytest.mark.parametrize("index", range(6))
def test_every_documented_under_cap_failure_shape_survives_a_strict_guard(index: int) -> None:
    """The isolated worker classifies by these shapes (`is_memory_failure`); a strict policy
    must hand the same exception class and text rules through, not a read error."""
    from decoy_engine.execution._isolated_common import is_memory_failure

    exc = _memory_shapes()[index]

    def boom() -> None:
        raise exc

    with pytest.raises(type(exc)) as ei:
        guarded("k", True, boom)
    assert is_memory_failure(ei.value)
    with pytest.raises(type(exc)):
        list(guarded_iter("k", True, _raising_iter(exc)))
