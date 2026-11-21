"""AT-V19: a stored v3 vault reads back through production code and through raw libsodium.

The fixture was written once by the production writer under mask key 32 bytes of 0x01, with two
entries and one row per segment, so it holds two real Parquet segments (message, then final).
The stored bytes are read and never re-written, so the test does not depend on the pyarrow
version that wrote them.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pyarrow.parquet as pq
from nacl import bindings

from decoy_engine.determinism import derive
from decoy_engine.internal.crypto import VAULT_KEY_CHECK_LABEL, key_check_value
from decoy_engine.vault import _format
from decoy_engine.vault._reader import _open_vault
from tests.unit.vault._support import split

FIXTURE = Path(__file__).parent / "fixtures" / "v3_known_answer.vault"
KEY = b"\x01" * 32
VAULT_ID = "d6cf8d63fd476ec459c1a2e04cbf7502b9a987c002deba08a8b1911699fd0d13"
STREAM_KEY = "6ac9021f1019f909de8000b6513b89d6baee0bf44424e3577e49745372e28be1"
KEY_CHECK = "a289a4ddd8001758"


def test_pinned_key_derivation() -> None:
    context = _format._stream_key_context(bytes.fromhex(VAULT_ID))
    assert derive(KEY, "vault", context).hex() == STREAM_KEY
    assert key_check_value(lambda: KEY, VAULT_KEY_CHECK_LABEL, VAULT_ID) == KEY_CHECK


def test_production_reader_returns_the_pinned_mapping() -> None:
    opened = _open_vault(FIXTURE.read_bytes(), lambda: KEY, "fixture")
    assert opened.mapping == {("ns", "masked-a"): "source-a", ("ns", "masked-b"): "source-b"}
    assert opened.ambiguous_dropped == 0
    assert opened.header.entry_count == 2
    assert opened.header.segment_rows == 1
    assert opened.header.vault_id == VAULT_ID
    assert opened.header.key_version == "v1"


def test_raw_libsodium_reads_the_same_plaintexts() -> None:
    blob = FIXTURE.read_bytes()
    header, segments = split(blob)
    assert len(segments) == 2
    start = len(b"DCYVAULT3\n")
    header_end = start + 4 + int.from_bytes(blob[start : start + 4], "big")
    ad = hashlib.sha256(blob[:header_end]).digest()  # the magic and header frame, as stored
    state = bindings.crypto_secretstream_xchacha20poly1305_state()
    bindings.crypto_secretstream_xchacha20poly1305_init_pull(
        state, bytes.fromhex(header["stream_header"]), bytes.fromhex(STREAM_KEY)
    )
    rows, tags = [], []
    for segment in segments:
        plaintext, tag = bindings.crypto_secretstream_xchacha20poly1305_pull(state, segment, ad)
        tags.append(tag)
        table = pq.read_table(io.BytesIO(plaintext))
        rows.extend(
            zip(
                *(table.column(n).to_pylist() for n in ("namespace", "masked", "source")),
                strict=True,
            )
        )
    assert tags == [
        bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE,
        bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL,
    ]
    assert rows == [("ns", "masked-a", "source-a"), ("ns", "masked-b", "source-b")]
