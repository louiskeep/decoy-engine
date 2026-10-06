"""A log-safe stand-in for a user expression.

`when:` predicates and transform expressions can embed literal values from the data
(`email == 'bob@example.com'`), and engine logs reach server logs that operators read.
Logs carry this short SHA-256 prefix instead, which is enough to match log lines to a
config without revealing its literals.
"""

from __future__ import annotations

import hashlib


def expression_fingerprint(expression: str) -> str:
    """The first 12 hex characters of the expression's SHA-256 digest."""
    return hashlib.sha256(expression.encode("utf-8")).hexdigest()[:12]
