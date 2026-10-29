"""RFC 8785 (JCS) canonical bytes for the signed profile.

Pattern: RFC 8785 JSON Canonicalization Scheme via the maintained ``rfc8785`` package (Trail of
Bits); property ordering by UTF-16 code units and ECMAScript number formatting are its job, not
ours. ``sort_keys=True`` is not JCS and is never used for a value that is signed.
"""

from __future__ import annotations

from typing import Any

import rfc8785

from ._errors import SignedEvidenceError


def canonical_bytes(value: Any) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError, RecursionError, UnicodeError):
        raise SignedEvidenceError("CANONICALIZATION_FAILED") from None
