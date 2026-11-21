"""The v2 reader kept beside v3, the frozen `VaultWriter(bytes)` call, and the read-error text."""

from __future__ import annotations

import traceback
from pathlib import Path

import pytest

from decoy_engine.fileaccess import FileHandle
from decoy_engine.vault import VAULT_FORMAT_VERSION, VaultError, VaultWriter, load_vault, open_vault
from tests.unit.vault._support import KEY, ROWS, prepared, split

CORPUS = Path(__file__).parents[2] / "integration" / "compat_corpus"
V2_FILE = CORPUS / "decoy-vault-v2__synthetic.vault"
V2_KEY = bytes.fromhex("c0ffee0badc0de01c0ffee0badc0de02c0ffee0badc0de03c0ffee0badc0de04")
V2_ROWS = {
    ("person_identity", "Jordan Avery"): "Maria Gonzalez",
    ("person_identity", "Riley Chen"): "David Okafor",
    ("member_id", "MBR-100001"): "843-22-9981",
    ("member_id", "MBR-100002"): "771-04-3325",
    ("email_pool", "user01@example.net"): "real.person@hospital.org",
}


def test_v2_vault_still_reads() -> None:
    mapping, ambiguous = load_vault(V2_FILE, V2_KEY)
    assert mapping == V2_ROWS
    assert ambiguous == 0


def test_v2_header_reports_its_format() -> None:
    opened = open_vault(V2_FILE, prepared(V2_KEY))
    assert opened.header.format == "decoy-vault/v2"
    assert opened.header.entry_count == len(V2_ROWS)


def test_v2_wrong_key_and_tamper_are_refused(tmp_path: Path) -> None:
    with pytest.raises(VaultError) as info:
        load_vault(V2_FILE, KEY)
    assert info.value.code == "vault_key_mismatch"
    blob = bytearray(V2_FILE.read_bytes())
    blob[-1] ^= 0xFF
    bad = tmp_path / "bad.vault"
    bad.write_bytes(bytes(blob))
    with pytest.raises(VaultError) as info:
        load_vault(bad, V2_KEY)
    assert info.value.code == "vault_key_mismatch"
    cut = tmp_path / "cut.vault"
    cut.write_bytes(V2_FILE.read_bytes()[:-5])
    with pytest.raises(VaultError) as info:
        load_vault(cut, V2_KEY)
    assert info.value.code == "vault_unreadable"


def test_v2_weak_key_is_refused_before_the_file_is_read() -> None:
    with pytest.raises(VaultError) as info:
        load_vault(V2_FILE, b"\x01" * 8)
    assert info.value.code == "weak_mask_secret"


def test_writes_stay_v3() -> None:
    writer = VaultWriter(KEY)
    writer.add(ROWS)
    blob, _ = writer.serialize()
    assert split(blob)[0]["format"] == VAULT_FORMAT_VERSION == "decoy-vault/v3"


def test_a_v2_leaf_is_read_but_never_own(tmp_path: Path) -> None:
    from decoy_engine.publication import _vault_order

    writer = VaultWriter(V2_KEY)
    writer.add([(ns, m, src) for (ns, m), src in V2_ROWS.items()])
    assert writer.reads_as_own(_vault_order.BytesHandle(V2_FILE.read_bytes())) is False
    blob, _ = writer.serialize()
    assert writer.reads_as_own(_vault_order.BytesHandle(blob)) is True


def test_bytes_key_gives_the_same_vault_as_a_prepared_secret(tmp_path: Path) -> None:
    from_bytes = VaultWriter(KEY)
    from_prepared = VaultWriter(prepared(KEY))
    from_bytes.add(ROWS)
    from_prepared.add(ROWS)
    one, count_one = from_bytes.serialize()
    two, count_two = from_prepared.serialize()
    assert count_one == count_two == len(ROWS)
    one_header, two_header = split(one)[0], split(two)[0]
    for field in ("format", "key_version", "entry_count", "ambiguous_dropped", "segment_rows"):
        assert one_header[field] == two_header[field]
    path = tmp_path / "a.vault"
    from_bytes.write(path)
    assert load_vault(path, KEY)[0] == {(ns, m): src for ns, m, src in ROWS}
    # each file opens under the other writer's key object: the key is the same
    assert from_bytes.reads_as_own(_handle(two)) and from_prepared.reads_as_own(_handle(one))
    from_bytes.assert_keyed_with(KEY)


def test_bytearray_key_is_accepted_and_weak_bytes_refused() -> None:
    VaultWriter(bytearray(KEY)).close()  # type: ignore[arg-type]  # the old call took any buffer
    with pytest.raises(VaultError) as info:
        VaultWriter(b"\x01" * 8)
    assert info.value.code == "weak_mask_secret"


def _handle(blob: bytes) -> FileHandle:
    from decoy_engine.publication import _vault_order

    return _vault_order.BytesHandle(blob)


class _FailingHandle:
    """A handle whose read raises an OSError that carries a secret in its text."""

    strict = False
    display = "<vault>"

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def read_all(self, limit: int | None = None) -> bytes:
        raise OSError(f"s3://user:{self._secret}@bucket/vault failed")

    def close(self) -> None:
        pass


def test_oserror_text_never_reaches_the_vault_error() -> None:
    secret = "PLANTED-SECRET-VALUE-9f3"
    with pytest.raises(VaultError) as info:
        load_vault(None, KEY, vault=_FailingHandle(secret))  # type: ignore[arg-type]
    err = info.value
    assert err.code == "vault_unreadable"
    assert secret not in str(err) and secret not in err.message
    assert err.__cause__ is None and err.__context__ is None
    assert secret not in "".join(traceback.format_exception(err))
