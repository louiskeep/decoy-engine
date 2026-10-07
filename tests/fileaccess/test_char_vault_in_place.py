"""E1-0 characterization: the ambient vault write truncates and rewrites the named file in
place. Green on the base; the port keeps it for the ambient policy."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from decoy_engine.vault import VaultWriter, load_vault

KEY = b"12345678"


def _writer() -> VaultWriter:
    writer = VaultWriter(KEY)
    writer.add([("ns", "m", "s")])
    return writer


def test_a_hardlinked_vault_is_written_through(tmp_path: Path) -> None:
    vault = tmp_path / "v.dcyvault"
    vault.write_bytes(b"old")
    twin = tmp_path / "twin"
    os.link(vault, twin)
    _writer().write(vault)
    assert twin.read_bytes() == vault.read_bytes() != b"old"
    assert load_vault(twin, KEY)[0] == {("ns", "m"): "s"}


def test_a_writable_vault_in_a_read_only_directory_is_rewritten(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    directory = tmp_path / "ro"
    directory.mkdir()
    vault = directory / "v.dcyvault"
    vault.write_bytes(b"old")
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        _writer().write(vault)
    finally:
        directory.chmod(stat.S_IRWXU)
    assert load_vault(vault, KEY)[0] == {("ns", "m"): "s"}


def test_a_missing_parent_names_the_vault_path_and_leaves_nothing(tmp_path: Path) -> None:
    target = tmp_path / "nope" / "v.dcyvault"
    with pytest.raises(FileNotFoundError) as info:
        _writer().write(target)
    assert str(target) in str(info.value)
    assert not (tmp_path / "nope").exists()
    assert [p.name for p in tmp_path.iterdir()] == []


def test_no_stage_file_is_left_beside_a_written_vault(tmp_path: Path) -> None:
    _writer().write(tmp_path / "v.dcyvault")
    assert [p.name for p in tmp_path.iterdir()] == ["v.dcyvault"]
