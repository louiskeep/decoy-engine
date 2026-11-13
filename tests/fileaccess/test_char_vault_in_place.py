"""E1-0 characterization of the ambient vault write, after B-6 D1.

The two pins of the in-place rewrite (a hardlinked vault written through, a writable vault in a
read-only directory rewritten) were replaced by
`tests/unit/test_vault_publish.py::test_an_existing_hardlinked_vault_is_refused_and_both_names_are_unchanged`
and `::test_an_existing_vault_in_a_read_only_parent_is_preserved_and_no_stage_remains`: the
vault is now staged and linked exclusively, never rewritten in place."""

from __future__ import annotations

from pathlib import Path

import pytest

from decoy_engine.vault import VaultWriter

KEY = b"12345678" * 4


def _writer() -> VaultWriter:
    writer = VaultWriter(KEY)
    writer.add([("ns", "m", "s")])
    return writer


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
