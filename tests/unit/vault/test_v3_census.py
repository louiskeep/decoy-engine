"""AT-V16: the secret-material census does not grow with the v3 vault.

Key material reaches the new code only as a `PreparedSecret` or a key-source callable, so the
vault files hold exactly the six entries below. Five are kept from the module they moved from; the
sixth is the `bytes` key that `VaultWriter(bytes)` wraps into a `PreparedSecret`, wiped on any raise.
A new entry here is a review decision, not a side effect.
"""

from __future__ import annotations

import json
from pathlib import Path

CENSUS = Path(__file__).resolve().parents[2] / "sentry" / "secret_material_census.json"

_EXPECTED = {
    ("decoy_engine/vault/_reader.py", "load_vault", "key"),
    ("decoy_engine/vault/_reader.py", "load_vault", "mask_key"),
    ("decoy_engine/vault/_writer.py", "VaultWriter.assert_keyed_with", "mask_key"),
    ("decoy_engine/vault/_writer.py", "_prepared_from_key", "mask_key"),
    ("decoy_engine/vault/_writer.py", "assert_vault_writer_keyed", "key"),
    ("decoy_engine/vault/_writer.py", "assert_vault_writer_keyed", "mask_key"),
}


def test_vault_census_entries_are_the_six_reviewed_ones() -> None:
    entries = json.loads(CENSUS.read_text(encoding="utf-8"))["entries"]
    found = {
        (e["file"], e["function"], e["name"])
        for e in entries
        if e["file"].startswith("decoy_engine/vault/")
    }
    assert found == _EXPECTED


def test_no_key_check_entries() -> None:
    entries = json.loads(CENSUS.read_text(encoding="utf-8"))["entries"]
    assert not [e for e in entries if e["function"] in {"key_check_value", "_open_vault"}]
