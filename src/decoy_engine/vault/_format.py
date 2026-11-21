"""The vault file layout and its crypto (`decoy-vault/v3`): the one module that knows either.

Pattern: libsodium `crypto_secretstream_xchacha20poly1305` through PyNaCl's `nacl.bindings`
(https://doc.libsodium.org/secret-key_cryptography/secretstream). The stream does the segment
encryption, the nonces, the counters and the end-of-stream marker: segments cannot be truncated,
removed, reordered, duplicated or modified without the pull failing. Decoy adds only a header
bound to every segment as additional data, a per-vault key from `derive`, a key check value, and
the length prefix the stream lacks. No cipher or AEAD construction of ours exists here.

Nothing outside this module imports `nacl` or knows the magic, the header declaration or the vault
key label (`tests/sentry/test_vault_crypto_choke_point.py`). The package docstring has the model.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine._canonical import (
    CountSpec,
    ExactSpec,
    ObjectSpec,
    ShapedSpec,
    Spec,
    VersionSpec,
)
from decoy_engine.determinism import SEED_PROTOCOL_VERSION, derive
from decoy_engine.errors import DecoyError
from decoy_engine.internal.crypto import KEY_CHECK_PATTERN
from decoy_engine.keyprovider import KEY_VERSION_PATTERN, is_key_version_label

VAULT_FORMAT_VERSION = "decoy-vault/v3"
VAULT_NAMESPACE = "vault"
# The per-vault key label. `_stream_key_context` appends `vault_id`, so `v2` names "one key per
# vault", where `v1` was "one key per secret".
VAULT_KEY_LABEL: bytes = b"vault-key/v2"

# decoy-vault/v3 framing: magic, a length-prefixed header (canonical JSON), then one
# length-prefixed secretstream ciphertext per segment, the last pushed with the final tag. The
# 4-byte big-endian length prefix is the idiom of determinism/_derive.py.
_MAGIC = b"DCYVAULT3\n"
_ANY_MAGIC = re.compile(rb"DCYVAULT[0-9]+\n")
_HEADER_MAX_BYTES = 4096
_SEGMENT_ROWS = 65536
_SEGMENT_MAX_ROWS = 1_048_576
_SEGMENT_MAX_BYTES = 64 * 1024 * 1024
_STREAM_OVERHEAD = 17  # libsodium secretstream ABYTES: the tag byte plus the 16-byte MAC
_VAULT_ID_BYTES = 32
_MAX_COUNT = 2**53 - 1
_MAX_INT_DIGITS = 20

_SCHEMA = pa.schema([(n, pa.string()) for n in ("namespace", "masked", "source")])


class VaultError(DecoyError):
    """Vault read/write failure. Machine-readable code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _unreadable(message: str) -> VaultError:
    return VaultError(code=reasons.runtime.VAULT_UNREADABLE, message=message)


def _tampered() -> VaultError:
    # Fixed text: a refusal names no path, value, key or check value.
    return VaultError(
        code=reasons.runtime.VAULT_TAMPERED,
        message=(
            "the vault file was changed or cut short after it was written; restore the copy "
            "you stored."
        ),
    )


# ---- the header: one declaration for the writer and the reader -------------------------------


@dataclass(frozen=True)
class _HeaderRecord:
    format: str
    seed_protocol_version: int
    key_version: str
    key_check: str
    vault_id: str
    stream_header: str
    ambiguous_dropped: int
    entry_count: int
    segment_rows: int


# `Pattern.fullmatch`, not a `$` anchor: `$` also matches before a trailing newline.
_VAULT_ID_PATTERN: Final = r"[0-9a-f]{64}"
_STREAM_HEADER_PATTERN: Final = r"[0-9a-f]{48}"

_HEADER_FIELDS: Final[tuple[tuple[str, Spec], ...]] = (
    ("format", ExactSpec(VAULT_FORMAT_VERSION)),
    ("seed_protocol_version", VersionSpec(SEED_PROTOCOL_VERSION)),
    ("key_version", ShapedSpec(KEY_VERSION_PATTERN, is_key_version_label)),
    ("key_check", ShapedSpec(KEY_CHECK_PATTERN, re.compile(KEY_CHECK_PATTERN).fullmatch)),
    ("vault_id", ShapedSpec(_VAULT_ID_PATTERN, re.compile(_VAULT_ID_PATTERN).fullmatch)),
    (
        "stream_header",
        ShapedSpec(_STREAM_HEADER_PATTERN, re.compile(_STREAM_HEADER_PATTERN).fullmatch),
    ),
    ("ambiguous_dropped", CountSpec(_MAX_COUNT)),
    ("entry_count", CountSpec(_MAX_COUNT)),
    ("segment_rows", CountSpec(_SEGMENT_MAX_ROWS)),
)
_HEADER = ObjectSpec(_HeaderRecord, _HEADER_FIELDS)


# ---- framing ---------------------------------------------------------------------------------


def _frame(block: bytes) -> bytes:
    """Length-prefix a block: 4-byte big-endian length || block."""
    return len(block).to_bytes(4, "big") + block


def _max_frame_bytes() -> int:
    """The longest segment frame a reader accepts, read at call time so a test can patch the cap."""
    return _SEGMENT_MAX_BYTES + _STREAM_OVERHEAD


def _read_frame(blob: bytes, offset: int, limit: int) -> tuple[bytes, int] | None:
    """One length-prefixed block at `offset` as (block, next_offset); None when it is cut short
    or longer than `limit`. The caller owns the refusal code, because it depends on how far the
    reader got."""
    if offset + 4 > len(blob):
        return None
    length = int.from_bytes(blob[offset : offset + 4], "big")
    start = offset + 4
    if length > limit or start + length > len(blob):
        return None
    return blob[start : start + length], start + length


# ---- the key floor ---------------------------------------------------------------------------

# Every provider's mask root is 32 bytes of HKDF output, and `derive` takes no other strong key
# length, so a longer key would pass a minimum and then fail at the first write or load.
_VAULT_KEY_BYTES = 32

_WEAK_KEY_MESSAGE = (
    f"the vault key must be the {_VAULT_KEY_BYTES}-byte mask root of a secret, as bytes; a "
    "vault is never keyed on the job seed. Build the writer with "
    "vault_writer_for_config(config, key_provider=...) or global_settings.mask_secret_ref."
)


def _key_size(key: object) -> int:
    # A `str` or any other non-bytes key counts as size 0, so it fails the floor like a short key.
    return len(key) if isinstance(key, (bytes, bytearray)) else 0


def _require_vault_key(size: int) -> None:
    """Refuse with `weak_mask_secret` unless the vault key's size (`_key_size`) is the mask root's.

    Callers pass only the size, so the key never becomes a local of this frame. The refusal text
    is fixed.
    """
    if size != _VAULT_KEY_BYTES:
        raise VaultError(code=reasons.runtime.WEAK_MASK_SECRET, message=_WEAK_KEY_MESSAGE)


# ---- the stream ------------------------------------------------------------------------------


def _sodium() -> Any:
    try:
        from nacl import bindings
    except ImportError as exc:
        raise VaultError(
            code=reasons.runtime.VAULT_CRYPTO_NOT_INSTALLED,
            message="the vault needs the PyNaCl package, which is part of Decoy; reinstall Decoy.",
        ) from exc
    return bindings


def _check_key(key_source: Callable[[], bytes]) -> None:
    """The crypto import, then the key floor: both run before any vault file is read."""
    _sodium()
    _require_vault_key(_key_size(key_source()))


def _stream_key_context(vault_id: bytes) -> bytes:
    return VAULT_KEY_LABEL + len(vault_id).to_bytes(4, "big") + vault_id


def _open_stream(key_source: Callable[[], bytes], vault_id: bytes) -> tuple[Any, bytes]:
    """A push state and its 24-byte stream header, under the key of the vault `vault_id`.

    The derived key goes straight into `init_push` and is never bound to a name.
    """
    bindings = _sodium()
    state = bindings.crypto_secretstream_xchacha20poly1305_state()
    header: bytes = bindings.crypto_secretstream_xchacha20poly1305_init_push(
        state, derive(key_source(), VAULT_NAMESPACE, _stream_key_context(vault_id))
    )
    return state, header


def _open_pull(key_source: Callable[[], bytes], vault_id: bytes, stream_header: bytes) -> Any:
    bindings = _sodium()
    state = bindings.crypto_secretstream_xchacha20poly1305_state()
    bindings.crypto_secretstream_xchacha20poly1305_init_pull(
        state, stream_header, derive(key_source(), VAULT_NAMESPACE, _stream_key_context(vault_id))
    )
    return state


def _push_segment(state: Any, plaintext: bytes, ad: bytes, final: bool) -> bytes:
    bindings = _sodium()
    tag = (
        bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL
        if final
        else bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE
    )
    cipher: bytes = bindings.crypto_secretstream_xchacha20poly1305_push(state, plaintext, ad, tag)
    return cipher


def _pull_segment(state: Any, frame: bytes, ad: bytes) -> tuple[bytes, bool] | None:
    """(plaintext, is_final) for the next segment; None when it does not authenticate (changed,
    out of order, or from another stream) or carries a tag the writer never uses. The caller
    raises, outside this handler, so the refusal has no cause or context."""
    bindings = _sodium()
    from nacl.exceptions import CryptoError

    try:
        plaintext, tag = bindings.crypto_secretstream_xchacha20poly1305_pull(state, frame, ad)
    except (CryptoError, ValueError):
        return None
    if tag == bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL:
        return plaintext, True
    if tag == bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE:
        return plaintext, False
    return None


# ---- segments --------------------------------------------------------------------------------


def _encode_rows(rows: list[tuple[str, str, str]]) -> bytes:
    import pyarrow.parquet as pq

    table = pa.Table.from_pylist(
        [dict(zip(_SCHEMA.names, r, strict=True)) for r in rows], schema=_SCHEMA
    )
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf)
    return bytes(buf.getvalue().to_pybytes())


def _decode_rows(payload: bytes) -> list[tuple[str, str, str]] | None:
    """The rows of one segment; None when it is not a table of the vault schema without nulls."""
    import pyarrow.parquet as pq

    try:
        chunk = pq.read_table(pa.BufferReader(payload))
        if chunk.schema.equals(_SCHEMA) and not any(c.null_count for c in chunk.columns):
            return [
                (str(ns), str(masked), str(source))
                for ns, masked, source in zip(*(c.to_pylist() for c in chunk.columns), strict=True)
            ]
    except (pa.ArrowException, KeyError, ValueError):
        pass
    return None


def _segment_frames(
    state: Any, rows: list[tuple[str, str, str]], ad: bytes, segment_rows: int
) -> Iterator[bytes]:
    """The framed ciphertext of every segment, the last pushed with the final tag.

    Each window of at most `segment_rows` rows is serialized and pushed on its own, so the full
    plaintext table is never one blob (F13). A window whose Parquet exceeds the byte cap is halved
    and retried; one entry that exceeds it alone is refused before any file exists. A vault with
    no entries still gets one final segment, so a file with no segment is always a truncation.
    """
    start, size = 0, segment_rows
    while True:
        window = rows[start : start + size]
        payload = _encode_rows(window)
        if len(payload) > _SEGMENT_MAX_BYTES:
            if len(window) == 1:
                raise VaultError(
                    code=reasons.runtime.VAULT_ENTRY_TOO_LARGE,
                    message=reasons.get("runtime", reasons.runtime.VAULT_ENTRY_TOO_LARGE).summary,
                )
            size = max(1, len(window) // 2)
            continue
        start += len(window)
        final = start >= len(rows)
        yield _frame(_push_segment(state, payload, ad, final))
        if final:
            return
