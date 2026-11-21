"""The read-only `decoy-vault/v2` reader (Fernet), kept so a vault written before v3 still unmasks.

Nothing writes v2. `_reader._open_vault` hands a blob here when it starts with the v2 magic, and
this module owns the v2 magic, the v2 key label and the Fernet import (the sentry
`tests/sentry/test_vault_crypto_choke_point.py` pins all three to this file). Refusals are fixed
text and are raised outside any handler, so no cause or context carries values.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any, Final

from decoy_engine import reasons
from decoy_engine._canonical import parse_or_token
from decoy_engine.determinism import SEED_PROTOCOL_VERSION, derive
from decoy_engine.vault._format import (
    _HEADER_MAX_BYTES,
    _MAX_INT_DIGITS,
    VAULT_NAMESPACE,
    VaultError,
    _decode_rows,
    _read_frame,
    _unreadable,
)

V2_FORMAT: Final = "decoy-vault/v2"
V2_MAGIC: Final = b"DCYVAULT2\n"
# The v2 key model: one key per secret, derived under this label.
_V2_KEY_LABEL: Final = b"vault-key/v1"
_FRAME_LIMIT: Final = 2**32 - 1


def read_v2(
    blob: bytes, key_source: Callable[[], bytes], shown: str
) -> tuple[dict[tuple[str, str], str], int, int]:
    """(map, ambiguous_dropped, chunk_rows) of a v2 vault.

    Raises:
        VaultError: ``vault_unreadable`` for a bad header, a cut-short frame or a malformed chunk;
            ``vault_format_unsupported`` for another format in the header;
            ``vault_protocol_version_mismatch`` for another seed protocol;
            ``vault_key_mismatch`` when the key does not decrypt a chunk.
    """
    header_frame = _read_frame(blob, len(V2_MAGIC), _HEADER_MAX_BYTES)
    if header_frame is None:
        raise _unreadable(f"vault {shown!r} has an unreadable header")
    header_bytes, offset = header_frame
    header, token = parse_or_token(header_bytes, _MAX_INT_DIGITS)
    if token is not None or type(header) is not dict:
        raise _unreadable(f"vault {shown!r} has an unreadable header")
    ambiguous = header.get("ambiguous_dropped", 0)
    chunk_rows = header.get("chunk_rows", 0)
    if type(ambiguous) is not int or type(chunk_rows) is not int:  # also bool, str, null
        raise _unreadable(f"vault {shown!r} has an unreadable header")
    if header.get("format") != V2_FORMAT:
        raise VaultError(
            code=reasons.runtime.VAULT_FORMAT_UNSUPPORTED,
            message=(
                f"vault {shown!r} declares a format this engine does not consume. Re-run the "
                "mask with --vault under this version."
            ),
        )
    if header.get("seed_protocol_version") != SEED_PROTOCOL_VERSION:
        raise VaultError(
            code=reasons.runtime.VAULT_PROTOCOL_VERSION_MISMATCH,
            message=(
                f"vault {shown!r} was written under another seed protocol version. "
                "Cross-version unmask is not supported; re-mask under this engine."
            ),
        )
    fernet = _fernet(key_source)
    mapping: dict[tuple[str, str], str] = {}
    while offset < len(blob):
        frame = _read_frame(blob, offset, _FRAME_LIMIT)
        if frame is None:
            raise _unreadable(f"vault {shown!r} is cut short")
        token_bytes, offset = frame
        payload = _decrypt(fernet, token_bytes)
        if payload is None:
            raise VaultError(
                code=reasons.runtime.VAULT_KEY_MISMATCH,
                message=(
                    "the key does not decrypt this vault; open it with the mask secret "
                    "of the run that wrote it."
                ),
            )
        rows = _decode_rows(payload)
        if rows is None:
            raise _unreadable("vault chunk is malformed")
        known = len(mapping)
        for ns, masked, source in rows:
            mapping[(ns, masked)] = source
        if len(mapping) != known + len(rows):  # the writer folded duplicates, so one is corruption
            raise _unreadable("vault holds a duplicate entry")
    return mapping, ambiguous, chunk_rows


def _fernet(key_source: Callable[[], bytes]) -> Any:
    from cryptography.fernet import Fernet

    # The derived key goes straight into Fernet and is never bound to a name.
    return Fernet(base64.urlsafe_b64encode(derive(key_source(), VAULT_NAMESPACE, _V2_KEY_LABEL)))


def _decrypt(fernet: Any, sealed: bytes) -> bytes | None:
    """The chunk plaintext, or None when it does not authenticate; the caller raises."""
    from cryptography.fernet import InvalidToken

    try:
        payload: bytes = fernet.decrypt(sealed)
    except InvalidToken:
        return None
    return payload
