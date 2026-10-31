"""Decision 1 (A-4 x F13): the `file:` secret provider reads through the installed policy.

F13's `FileSecretProvider` opened the secret with a bare `open()`, outside the file-access
policy. These cells show the read is a `FileRole.SECRET` request at the config position, on both
public routes (`key_provider_from_ref` and `admit_mask_key`), and that the E1 owner-only check
also runs on a descriptor a strict policy issued.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileAccessDenied, FileRole
from decoy_engine.keyprovider import MaskSecretError, key_provider_from_ref
from decoy_engine.secret_providers import admit_mask_key
from tests.fileaccess.support import RecordingFileAccess, RootRelative

FIELD = "global_settings.mask_secret_ref"
GOOD = ("ab" * 32).encode()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "root"
    path.mkdir()
    return path


@pytest.fixture
def recorded(root: Path) -> Iterator[RecordingFileAccess]:
    inner = RootRelative(root)
    try:
        yield RecordingFileAccess(inner)
    finally:
        inner.close()


def _secret(folder: Path, mode: int = 0o600, name: str = "k.key") -> Path:
    path = folder / name
    path.write_bytes(GOOD)
    os.chmod(path, mode)
    return path


def test_key_provider_from_ref_reads_through_the_policy(
    root: Path, recorded: RecordingFileAccess
) -> None:
    key = key_provider_from_ref(f"file:{_secret(root)}", file_access=recorded).mask_key()
    assert len(key) == 32
    assert recorded.requests == [(FileRole.SECRET, "read", FIELD, "granted")]


def test_admission_reads_through_the_installed_policy(
    root: Path, recorded: RecordingFileAccess
) -> None:
    config = {"global_settings": {"mask_secret_ref": f"file:{_secret(root)}"}, "tables": []}
    with fileaccess.scope(recorded):
        admit_mask_key(config).close()
    assert recorded.requests == [(FileRole.SECRET, "read", FIELD, "granted")]


def test_admission_outside_the_root_is_denied_by_the_policy(
    tmp_path: Path, recorded: RecordingFileAccess
) -> None:
    config = {"global_settings": {"mask_secret_ref": f"file:{_secret(tmp_path)}"}, "tables": []}
    with fileaccess.scope(recorded), pytest.raises(FileAccessDenied):
        admit_mask_key(config)
    assert recorded.requests == [(FileRole.SECRET, "read", FIELD, "denied")]


def test_the_owner_only_check_runs_on_a_policy_issued_descriptor(
    root: Path, recorded: RecordingFileAccess
) -> None:
    unsafe = _secret(root, 0o644, "loose.key")
    with pytest.raises(MaskSecretError) as info:
        key_provider_from_ref(f"file:{unsafe}", file_access=recorded)
    assert info.value.code == "secret_file_permissions_unsafe"
    assert recorded.requests == [(FileRole.SECRET, "read", FIELD, "granted")]
