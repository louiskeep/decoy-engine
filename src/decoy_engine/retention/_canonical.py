"""Canonical JSON and identifier validation shared by the policy, report and digest.

One byte-exact encoding (sorted keys, no whitespace, ASCII-only) so a report re-read from storage
re-serializes identically and a digest computed on two machines agrees.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")
_ERROR_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_POLICY_ID = re.compile(r"^[0-9a-f]{16}$")


def canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def is_opaque_id(value: object) -> bool:
    """Opaque identifiers are 32 lowercase hex characters: a UUID without hyphens, or a
    truncated digest. Anything else could carry a name or path and is rejected."""
    return isinstance(value, str) and _OPAQUE_ID.fullmatch(value) is not None


def check_opaque_id(name: str, value: object) -> str:
    if not is_opaque_id(value):
        raise ValueError(f"{name} must be a 32-character lowercase hex identifier")
    return value  # type: ignore[return-value]


def check_error_type(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _ERROR_TYPE.fullmatch(value) is None:
        raise ValueError("error_type must be an identifier of at most 64 characters")
    return value


def check_policy_id(value: object) -> str:
    if not isinstance(value, str) or _POLICY_ID.fullmatch(value) is None:
        raise ValueError("policy_id must be 16 lowercase hex characters")
    return value


def check_count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value
