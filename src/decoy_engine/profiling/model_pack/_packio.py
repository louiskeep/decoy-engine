"""Model-pack file I/O for the offline tooling (training, signing, test loading).

A pack directory a caller names is a directory-valued input, so it is read through a
directory capability and written one file at a time as an output transaction that replaces
the file. Nothing here opens a path itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from decoy_engine import fileaccess
from decoy_engine.fileaccess import DirHandle, FileAccess, FileRequest, FileRole

#: Config position a model-pack diagnostic names the directory by (X2).
_PACK_POSITION = "pre_scan.model_pack"


def member_display(pack_dir: Path) -> str:
    """How a diagnostic names the pack: its path on the ambient CLI, the config position under a
    strict policy. Readers (`trainer`, `provenance`) call this instead of naming `pack_dir` raw."""
    return fileaccess.shown(str(pack_dir), fileaccess.position_key(_PACK_POSITION))


def open_pack_dir(pack_dir: Path, file_access: FileAccess | None = None) -> DirHandle:
    policy = fileaccess.capture(file_access)
    return policy.open_dir(
        FileRequest(FileRole.MODEL_PACK_ROOT, "read_dir", str(pack_dir), "pre_scan.model_pack")
    )


def read_member(pack_dir: Path, name: str, file_access: FileAccess | None = None) -> bytes | None:
    """The bytes of one file of the pack, or None when it is absent."""
    directory = open_pack_dir(pack_dir, file_access)
    try:
        member = directory.open_file(name)
        try:
            return member.read_all()
        finally:
            member.close()
    except (FileNotFoundError, NotADirectoryError):
        return None
    finally:
        directory.close()


def write_member(
    pack_dir: Path, name: str, data: bytes, file_access: FileAccess | None = None
) -> None:
    """Write one file of the pack, replacing any existing one; the pack directory is created."""
    policy = fileaccess.capture(file_access)
    out = policy.output_dir(
        FileRequest(
            FileRole.MODEL_PACK_ROOT, "write", str(Path(pack_dir) / name), "pre_scan.model_pack"
        )
    )
    with out.begin("file", "replace", label="pack") as txn:
        with os.fdopen(txn.create_file(name), "wb") as fh:
            fh.write(data)
        txn.publish()


def write_json(pack_dir: Path, name: str, document: dict[str, object]) -> None:
    """Write `document` as canonical JSON (sorted keys, two-space indent) into the pack."""
    write_member(pack_dir, name, json.dumps(document, sort_keys=True, indent=2).encode("utf-8"))
