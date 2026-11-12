"""The one canonical JSON form and the two digests built on it.

Sorted keys, no whitespace, UTF-8 without ASCII escaping, and no NaN or infinity. This is the
form the CLI evidence manifest hash already uses (``ensure_ascii=False`` matches the platform's
canonical JSON), so a record id and a manifest hash agree on the bytes of the same content.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

__all__ = ["canonical_bytes", "payload_digest", "record_id", "sha256_tag"]


def canonical_bytes(value: Any) -> bytes:
    """The canonical UTF-8 bytes of a JSON value. Raises ``ValueError`` on NaN or infinity."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_tag(data: bytes) -> str:
    """``sha256:<hex>`` of ``data``."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def payload_digest(payload: Any) -> str:
    """The digest of the canonical payload alone: equal content gives an equal digest."""
    return sha256_tag(canonical_bytes(payload))


def record_id(envelope: Mapping[str, Any]) -> str:
    """The id of an envelope: the digest of its canonical form without ``id``.

    The one id rule. Parsing first and hashing the parsed value means a CRLF checkout or a
    reformatted file keeps the id of the record it holds.
    """
    return sha256_tag(canonical_bytes({k: v for k, v in envelope.items() if k != "id"}))
