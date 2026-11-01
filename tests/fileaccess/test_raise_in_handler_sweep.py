"""The sweep of `raise` inside `except` in fileaccess/ and secret_providers/.

`raise X from None` hides the handled exception from traceback printing but keeps it as
`__context__`, where an error tracker or `exc.__context__` still reaches it. Each site below
held an exception naming a path or carrying secret bytes; each now raises outside the handler.
"""

from __future__ import annotations

import os
import traceback
from pathlib import Path

import pytest

from decoy_engine.fileaccess import FileAccessDenied, FileRequest, FileRole, RootedFileAccess
from decoy_engine.fileaccess._local import LocalFileAccess
from decoy_engine.keyprovider import MaskSecretError
from decoy_engine.secret_providers import REQUIREMENTS, SecretMaterialInvalid
from decoy_engine.secret_providers._requirements import decode_material

CANARY = "SWEEP-CANARY"


def _texts(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) + repr(exc.args)


def test_rooted_emptiness_probe_denies_without_the_os_error(
    rooted: RootedFileAccess, root_dir: Path
) -> None:
    (root_dir / "out").mkdir()
    (root_dir / "out" / f"leaf-{CANARY}").write_text("a file, not a directory")
    out = rooted.output_dir(
        FileRequest(FileRole.TARGET_OUTPUT, "write", f"out/leaf-{CANARY}", "targets.t.path")
    )
    with pytest.raises(FileAccessDenied) as info:
        out.is_empty_or_absent()
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert CANARY not in _texts(info.value)


def test_ambient_exclusive_publish_onto_an_occupant_carries_no_link_error(tmp_path: Path) -> None:
    target = tmp_path / f"out-{CANARY}"
    target.write_text("occupant")
    out = LocalFileAccess().output_dir(
        FileRequest(FileRole.QUARANTINE_OUTPUT, "write", str(target), "quarantine.path")
    )
    txn = out.begin("file", "exclusive", label="quarantine")
    fd = txn.create_file("data")
    os.write(fd, b"new")
    os.close(fd)
    with pytest.raises(FileExistsError) as info:
        txn.publish()
    assert info.value.__context__ is None
    assert str(info.value) == "[Errno 17] output exists"
    assert target.read_text() == "occupant"


MASK_ROOT = REQUIREMENTS["decoy-mask-root-v1"]
FERNET = REQUIREMENTS["fernet-v1"]
SECRET_BYTES = b"\xff" + CANARY.encode()

DECODE_CASES = {
    "hex_not_ascii": (SECRET_BYTES, "hex", MASK_ROOT, MaskSecretError),
    "base64_bad": (CANARY.encode() + b"!!", "base64", FERNET, SecretMaterialInvalid),
    "legacy_not_utf8": (SECRET_BYTES, None, MASK_ROOT, MaskSecretError),
    "legacy_bad_base64": (CANARY.encode() + b"!!", None, MASK_ROOT, MaskSecretError),
    "legacy_empty": (b"  \n", None, MASK_ROOT, MaskSecretError),
    "needs_encoding": (CANARY.encode(), None, FERNET, MaskSecretError),
}


@pytest.mark.parametrize("case", sorted(DECODE_CASES))
def test_decode_material_refuses_bare(case: str) -> None:
    raw, encoding, req, error = DECODE_CASES[case]
    with pytest.raises(error) as info:
        decode_material(raw, encoding, req)
    exc = info.value
    assert exc.__context__ is None and exc.__cause__ is None
    assert CANARY not in _texts(exc)
    for function, name, text in _frame_texts(exc):
        assert CANARY not in text, f"{function}() local {name!r}"


@pytest.mark.parametrize(
    ("raw", "encoding", "expected"),
    [
        (b"6869", "hex", b"hi"),
        (b"aGk=", "base64", b"hi"),
        (b"raw-bytes", "raw", b"raw-bytes"),
        (b" 68 69 \n", None, b"hi"),
        (b"aGk=", None, b"hi"),
    ],
)
def test_decode_material_values_are_unchanged(
    raw: bytes, encoding: str | None, expected: bytes
) -> None:
    assert decode_material(raw, encoding, MASK_ROOT) == expected


def _frame_texts(exc: BaseException):  # type: ignore[no-untyped-def]
    from tests.fileaccess.locals_support import engine_locals

    yield from engine_locals(exc)
