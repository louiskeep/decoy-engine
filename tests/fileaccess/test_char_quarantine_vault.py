"""E1-0 characterization: the quarantine sidecar writers and the vault writer, no policy.

The quarantine helpers stage a JSONL file beside the final path and publish it with an
exclusive hardlink (refusing any existing file); the vault writer overwrites its target.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decoy_engine.quarantine import (
    discard_staged_jsonl,
    publish_staged_jsonl,
    write_jsonl_staged,
)
from decoy_engine.vault import VaultError, VaultWriter, load_vault

RECORDS = [{"a": 1, "b": "x"}, {"a": 2, "b": None}]


def _stages(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith("_decoy_quarantine_stage_"))


def test_staged_file_sits_beside_the_final_path_and_never_touches_it(tmp_path: Path) -> None:
    final = tmp_path / "deep" / "q.jsonl"
    staged = write_jsonl_staged(str(final), RECORDS)
    assert staged.parent == final.parent
    assert staged.name.startswith("_decoy_quarantine_stage_") and staged.name.endswith(".jsonl")
    assert not final.exists()
    assert [json.loads(x) for x in staged.read_text().splitlines()] == RECORDS
    assert (staged.stat().st_mode & 0o777) == 0o600


def test_publish_links_then_removes_the_stage(tmp_path: Path) -> None:
    final = tmp_path / "q.jsonl"
    staged = write_jsonl_staged(str(final), RECORDS)
    publish_staged_jsonl(staged, str(final))
    assert [json.loads(x) for x in final.read_text().splitlines()] == RECORDS
    assert _stages(tmp_path) == []


def test_publish_refuses_an_existing_file_and_discards_the_stage(tmp_path: Path) -> None:
    final = tmp_path / "q.jsonl"
    final.write_text("occupant")
    staged = write_jsonl_staged(str(final), RECORDS)
    with pytest.raises(ValueError) as info:
        publish_staged_jsonl(staged, str(final))
    assert str(info.value) == (
        f"refusing to publish quarantine: a file already exists at quarantine.output_path "
        f"{str(final)!r}; transactional publish will not overwrite it"
    )
    assert final.read_text() == "occupant"
    assert _stages(tmp_path) == []


def test_discard_is_idempotent_and_none_is_a_no_op(tmp_path: Path) -> None:
    staged = write_jsonl_staged(str(tmp_path / "q.jsonl"), RECORDS)
    discard_staged_jsonl(staged)
    discard_staged_jsonl(staged)
    discard_staged_jsonl(None)
    assert _stages(tmp_path) == []


def test_vault_write_overwrites_and_round_trips(tmp_path: Path) -> None:
    key = b"12345678"
    path = tmp_path / "v.dcyvault"
    first = VaultWriter(key)
    first.add([("ns", "m1", "s1")])
    assert first.write(path) == 1
    second = VaultWriter(key)
    second.add([("ns", "m2", "s2"), ("ns", "m3", "s3")])
    assert second.write(str(path)) == 2
    mapping, ambiguous = load_vault(path, key)
    assert mapping == {("ns", "m2"): "s2", ("ns", "m3"): "s3"} and ambiguous == 0


def test_vault_write_into_a_missing_directory_fails_with_the_os_error(tmp_path: Path) -> None:
    writer = VaultWriter(b"12345678")
    writer.add([("ns", "m", "s")])
    with pytest.raises(FileNotFoundError):
        writer.write(tmp_path / "nope" / "v.dcyvault")


def test_vault_wrong_key_is_a_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "v.dcyvault"
    writer = VaultWriter(b"12345678")
    writer.add([("ns", "m", "s")])
    writer.write(path)
    with pytest.raises(VaultError) as info:
        load_vault(path, b"87654321")
    assert info.value.code == "vault_key_mismatch"
