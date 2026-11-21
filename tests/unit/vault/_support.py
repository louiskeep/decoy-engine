"""Build vault writers from raw test keys, now that a `VaultWriter` takes a `PreparedSecret`."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from decoy_engine._canonical import canonical_json_bytes
from decoy_engine.determinism import SEED_PROTOCOL_VERSION
from decoy_engine.internal.crypto import VAULT_KEY_CHECK_LABEL, key_check_value
from decoy_engine.secret_providers import PreparedSecret
from decoy_engine.secret_providers._buffer import SecretBuffer
from decoy_engine.secret_providers._requirements import MASK_ROOT_V1
from decoy_engine.vault import VaultWriter, _format


def prepared(key: bytes, key_version: str = "v1") -> PreparedSecret:
    """A `PreparedSecret` over `key`, of any length, so weak-key cells can be built too."""
    return PreparedSecret(
        SecretBuffer.adopt(bytearray(key)), key_version=key_version, requirement_ids=(MASK_ROOT_V1,)
    )


def vault_writer(key: bytes, key_version: str = "v1", **kwargs: Any) -> VaultWriter:
    """A writer that owns its own clone; the helper's original is closed at once."""
    secret = prepared(key, key_version)
    try:
        return VaultWriter(secret, **kwargs)
    finally:
        secret.close()


# ---- raw v3 file helpers, for the format and tamper tests ------------------------------------


KEY = b"\x07" * 32
OTHER_KEY = b"\x08" * 32
ROWS = [
    ("email_ns", "masked-1", "SRC-ALPHA"),
    ("email_ns", "masked-2", "SRC-BRAVO"),
    ("email_ns", "masked-3", "SRC-CHARLIE"),
    ("email_ns", "masked-4", "SRC-DELTA"),
    ("email_ns", "masked-5", "SRC-ECHO"),
]


def written(rows: list[tuple[str, str, str]] = ROWS, key: bytes = KEY) -> bytes:
    writer = vault_writer(key)
    writer.add(rows)
    blob, _ = writer.serialize()
    return blob


def split(blob: bytes) -> tuple[dict, list[bytes]]:
    """The header dict and the raw (unframed) segment ciphertexts of a v3 file."""
    start = len(_format._MAGIC)
    length = int.from_bytes(blob[start : start + 4], "big")
    header = json.loads(blob[start + 4 : start + 4 + length])
    offset, segments = start + 4 + length, []
    while offset < len(blob):
        size = int.from_bytes(blob[offset : offset + 4], "big")
        segments.append(blob[offset + 4 : offset + 4 + size])
        offset += 4 + size
    return header, segments


def join(header: dict, segments: list[bytes]) -> bytes:
    """A file from a header dict and segments, re-framed; the ciphertexts are not touched."""
    body = canonical_json_bytes(header)
    return _format._MAGIC + _format._frame(body) + b"".join(_format._frame(s) for s in segments)


def forge(
    segments: list[list[tuple[str, str, str]]],
    tags: list[str],
    *,
    entry_count: int | None = None,
    key: bytes = KEY,
    segment_rows: int | None = None,
) -> bytes:
    """An authenticated v3 file built through the format seam, so it can be inconsistent.

    `tags` is one of "message", "final", "push", "rekey" per segment.
    """
    total = sum(len(s) for s in segments) if entry_count is None else entry_count
    return _forge([_format._encode_rows(rows) for rows in segments], tags, total, key, segment_rows)


def forge_payloads(payloads: list[bytes], *, key: bytes = KEY, entry_count: int = 2) -> bytes:
    """An authenticated v3 file with one segment per raw payload, the last one final."""
    tags = ["message"] * (len(payloads) - 1) + ["final"]
    return _forge(payloads, tags, entry_count, key)


def _forge(
    payloads: list[bytes],
    tags: list[str],
    entry_count: int,
    key: bytes,
    segment_rows: int | None = None,
) -> bytes:
    from nacl import bindings

    vault_id = os.urandom(32)
    state, stream_header = _format._open_stream(lambda: key, vault_id)
    record = _format._HeaderRecord(
        format=_format.VAULT_FORMAT_VERSION,
        seed_protocol_version=SEED_PROTOCOL_VERSION,
        key_version="v1",
        key_check=key_check_value(lambda: key, VAULT_KEY_CHECK_LABEL, vault_id.hex()),
        vault_id=vault_id.hex(),
        stream_header=stream_header.hex(),
        ambiguous_dropped=0,
        entry_count=entry_count,
        segment_rows=_format._SEGMENT_ROWS if segment_rows is None else segment_rows,
    )
    header_frame = _format._frame(canonical_json_bytes(_format._HEADER.dump(record)))
    ad = hashlib.sha256(_format._MAGIC + header_frame).digest()
    raw_tags = {
        "push": bindings.crypto_secretstream_xchacha20poly1305_TAG_PUSH,
        "rekey": bindings.crypto_secretstream_xchacha20poly1305_TAG_REKEY,
    }
    frames = []
    for payload, tag in zip(payloads, tags, strict=True):
        if tag in raw_tags:
            cipher = bindings.crypto_secretstream_xchacha20poly1305_push(
                state, payload, ad, raw_tags[tag]
            )
        else:
            cipher = _format._push_segment(state, payload, ad, tag == "final")
        frames.append(_format._frame(cipher))
    return _format._MAGIC + header_frame + b"".join(frames)


def read(blob: bytes, tmp_path: Path, key: bytes = KEY) -> tuple[dict, int]:
    """`load_vault` over a blob written to a temp file."""
    from decoy_engine.vault import load_vault

    path = tmp_path / "v.vault"
    path.write_bytes(blob)
    return load_vault(path, key)
