"""Read a vault file back into `{(namespace, masked): source}` plus its non-secret header."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from decoy_engine import fileaccess, reasons
from decoy_engine._canonical import attempt, parse_or_token
from decoy_engine._opaque import OpaqueBox
from decoy_engine.fileaccess import (
    FileAccess,
    FileAccessUnavailable,
    FileHandle,
    FileRequest,
    FileRole,
)
from decoy_engine.internal.crypto import VAULT_KEY_CHECK_LABEL, key_check_value
from decoy_engine.secret_providers import PreparedSecret, SecretClosedError
from decoy_engine.vault._format import (
    _ANY_MAGIC,
    _HEADER,
    _HEADER_MAX_BYTES,
    _MAGIC,
    _MAX_INT_DIGITS,
    VAULT_FORMAT_VERSION,
    VaultError,
    _check_key,
    _decode_rows,
    _HeaderRecord,
    _key_size,
    _max_frame_bytes,
    _open_pull,
    _pull_segment,
    _read_frame,
    _tampered,
    _unreadable,
)
from decoy_engine.vault._v2_reader import V2_FORMAT, V2_MAGIC, read_v2

_SEED_PROTOCOL_VERSION_KEY = "seed_protocol_version"


@dataclass(frozen=True)
class VaultHeader:
    """The non-secret header fields. The key check and the stream header are not exposed."""

    format: str
    key_version: str
    vault_id: str
    entry_count: int
    ambiguous_dropped: int
    segment_rows: int


@dataclass(frozen=True)
class OpenedVault:
    """A decrypted vault: its map, the dropped-ambiguous count, and the header it was read under."""

    mapping: dict[tuple[str, str], str]
    ambiguous_dropped: int
    header: VaultHeader


def _pull_all(
    blob: bytes,
    offset: int,
    key_source: Callable[[], bytes],
    record: _HeaderRecord,
    ad: bytes,
) -> dict[tuple[str, str], str] | None:
    """Every segment in order into a map; None on any damage (the caller raises `vault_tampered`).

    A segment that fails to authenticate, a frame over the cap, a tag other than message or final,
    a final tag before the last frame, bytes after it, no final tag, a segment over `segment_rows`,
    a duplicate or malformed row, or a row total other than `entry_count` all return None.
    """
    state = _open_pull(
        key_source, bytes.fromhex(record.vault_id), bytes.fromhex(record.stream_header)
    )
    mapping: dict[tuple[str, str], str] = {}
    final = False
    while offset < len(blob):
        if final:
            return None
        frame = _read_frame(blob, offset, _max_frame_bytes())
        if frame is None:
            return None
        block, offset = frame
        pulled = _pull_segment(state, block, ad)
        if pulled is None:
            return None
        plaintext, final = pulled
        rows = _decode_rows(plaintext)
        if rows is None or len(rows) > record.segment_rows:
            return None
        known = len(mapping)
        mapping.update({(ns, m): src for ns, m, src in rows})
        if len(mapping) != known + len(rows):  # the writer folds duplicates, so one is corruption
            return None
    if not final or len(mapping) != record.entry_count:
        return None
    return mapping


def _open_v2(blob: bytes, key_source: Callable[[], bytes], shown: str) -> OpenedVault:
    """A `decoy-vault/v2` file, read under the same key floor as v3. It has no key check, no
    vault id and no entry count; the header records what it can."""
    mapping, ambiguous, chunk_rows = read_v2(blob, key_source, shown)
    return OpenedVault(
        mapping,
        ambiguous,
        VaultHeader(
            format=V2_FORMAT,
            key_version="v1",
            vault_id="",
            entry_count=len(mapping),
            ambiguous_dropped=ambiguous,
            segment_rows=chunk_rows,
        ),
    )


def _open_vault(blob: bytes, key_source: Callable[[], bytes], shown: str) -> OpenedVault:
    """The one reader. Read order: magic, header frame, header rules, key check, then the stream.

    Before the header is understood a bad file is "unreadable"; once it is understood and the key
    check passes, any damage is "tampered". Every refusal is raised here, outside any handler, so
    it carries no cause or context.
    """
    if blob.startswith(V2_MAGIC):  # the one dispatch on the magic: v2 is read-only and kept
        return _open_v2(blob, key_source, shown)
    if not blob.startswith(_MAGIC):
        if _ANY_MAGIC.match(blob):
            raise VaultError(
                code=reasons.runtime.VAULT_FORMAT_UNSUPPORTED,
                message=(
                    f"vault {shown!r} was written by an older Decoy and this version cannot read "
                    f"it. Re-run the mask with --vault under this version to write a new vault."
                ),
            )
        raise _unreadable(f"{shown!r} is not a decoy vault file (bad magic header).")
    header_frame = _read_frame(blob, len(_MAGIC), _HEADER_MAX_BYTES)
    if header_frame is None:
        raise _unreadable(f"vault {shown!r} has an unreadable header")
    header_bytes, offset = header_frame
    parsed, token = parse_or_token(header_bytes, _MAX_INT_DIGITS)
    if token is not None:
        raise _unreadable(f"vault {shown!r} has an unreadable header")
    # `format` is judged before the closed field set, so a future format with other fields is
    # "unsupported", not "unreadable".
    if type(parsed) is dict and "format" in parsed and parsed["format"] != VAULT_FORMAT_VERSION:
        raise VaultError(
            code=reasons.runtime.VAULT_FORMAT_UNSUPPORTED,
            message=(
                f"vault {shown!r} declares a format this engine does not consume; it consumes "
                f"{VAULT_FORMAT_VERSION!r}. Re-run the mask with --vault under this version."
            ),
        )
    record, check = attempt(_HEADER.load, parsed)
    if check == "schema_unknown":  # `format` passed above, so only the protocol version is left
        written = parsed.get(_SEED_PROTOCOL_VERSION_KEY) if type(parsed) is dict else None
        raise VaultError(
            code=reasons.runtime.VAULT_PROTOCOL_VERSION_MISMATCH,
            message=(
                f"vault {shown!r} was written under seed protocol version "
                f"{written if type(written) is int else 'unknown'}, which this engine does not "
                f"run. Cross-version unmask is not supported; re-mask under this engine."
            ),
        )
    if check is not None or record.segment_rows == 0:
        raise _unreadable(f"vault {shown!r} has an unreadable header")

    expected_check = key_check_value(key_source, VAULT_KEY_CHECK_LABEL, record.vault_id)
    if not hmac.compare_digest(record.key_check, expected_check):
        raise VaultError(
            code=reasons.runtime.VAULT_KEY_MISMATCH,
            message=(
                f"this vault records key version {record.key_version!r}; open it with the mask "
                "secret of the run that wrote it. If that secret is right, the key_check or "
                "vault_id field of the header was edited: restore the stored copy."
            ),
        )
    ad = hashlib.sha256(blob[:offset]).digest()  # magic and header frame, as read
    mapping = _pull_all(blob, offset, key_source, record, ad)
    if mapping is None:
        raise _tampered()
    return OpenedVault(
        mapping,
        record.ambiguous_dropped,
        VaultHeader(
            format=record.format,
            key_version=record.key_version,
            vault_id=record.vault_id,
            entry_count=record.entry_count,
            ambiguous_dropped=record.ambiguous_dropped,
            segment_rows=record.segment_rows,
        ),
    )


def _read_and_open(
    path: str | Path | None,
    key_source: Callable[[], bytes],
    *,
    vault: FileHandle | None,
) -> OpenedVault:
    # The crypto import and the key floor come first, so a missing PyNaCl or a short key fails
    # even when the file is absent and before any file is touched.
    _check_key(key_source)
    handle = vault
    if handle is None:
        if path is None or not fileaccess.is_ambient():
            raise FileAccessUnavailable(
                "load_vault needs a vault handle from the policy; a path string is accepted "
                "only under the ambient policy"
            )
        handle = fileaccess.current().open_read(
            FileRequest(FileRole.VAULT, "read", str(path), "vault")
        )
    shown = handle.display
    unreadable = False
    try:
        blob = handle.read_all()
    except OSError as exc:
        if fileaccess.keeps_class(exc):
            raise
        unreadable = True  # the raise is below, outside the handler: no cause or context
    finally:
        if vault is None:
            handle.close()
    if unreadable:
        # Fixed text: an adapter's OSError can carry a path, a value or URL credentials.
        raise VaultError(
            code=reasons.runtime.VAULT_UNREADABLE,
            message=f"vault file {shown!r} could not be read.",
        )
    return _open_vault(blob, key_source, shown)


@fileaccess.entry_point
def load_vault(
    path: str | Path | None,
    mask_key: bytes,
    *,
    vault: FileHandle | None = None,
    file_access: FileAccess | None = None,
) -> tuple[dict[tuple[str, str], str], int]:
    """Decrypt a vault file into `{(namespace, masked): source}`.

    DE-02: `mask_key` is the keyed-mask IKM the vault was written under (the
    32-byte KeyProvider root). A shorter key, such as the 8-byte `job_seed`
    an older engine keyed vaults on, is refused before the file is read.

    `vault` is a handle the policy issued for the vault file and takes the place of `path`;
    a path string is accepted only under the ambient CLI policy and raises
    `FileAccessUnavailable` under any other.

    Returns the map plus the recorded `ambiguous_dropped` count.

    Raises:
        VaultError: ``vault_crypto_not_installed`` when PyNaCl is missing;
            ``weak_mask_secret`` when `mask_key` is not the 32-byte mask root, as bytes (checked
            before the file is read); ``vault_unreadable`` when the file is missing, not a vault
            or has a bad header; ``vault_format_unsupported`` on another vault format;
            ``vault_protocol_version_mismatch`` when the vault was written under a different
            `SEED_PROTOCOL_VERSION`; ``vault_key_mismatch`` when the key check fails (another
            secret, or an edited `key_check` or `vault_id`); ``vault_tampered`` when anything else
            in the file changed.
    """
    # The size is read inside the boxing statement, because the key must be boxed and dropped
    # before anything else runs.
    key = OpaqueBox(mask_key, _key_size(mask_key))
    del mask_key  # a traceback keeps this frame: the key must not be among its locals
    opened = _read_and_open(path, lambda: key.open()[0], vault=vault)
    return opened.mapping, opened.ambiguous_dropped


@fileaccess.entry_point
def open_vault(
    path: str | Path | None,
    prepared: PreparedSecret,
    *,
    vault: FileHandle | None = None,
    file_access: FileAccess | None = None,
) -> OpenedVault:
    """Like `load_vault`, keyed by a `PreparedSecret`, and returning the header with the map.

    The header and the map come from one read of the file.
    """
    if not isinstance(prepared, PreparedSecret):
        raise TypeError("open_vault takes a PreparedSecret")
    if prepared.closed:
        raise SecretClosedError
    return _read_and_open(path, prepared.mask_key, vault=vault)
