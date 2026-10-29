"""One typed error for the signed-evidence core.

The core ships standalone (no ``decoy_engine`` import), so it cannot inherit ``DecoyError``;
engine-facing callers wrap it. The message is the code alone: never an input value, so a hostile
document cannot reflect its content into logs or reports.
"""

from __future__ import annotations


class SignedEvidenceError(Exception):
    """A stable machine code plus an optional result status (MALFORMED, UNSUPPORTED, ...)."""

    def __init__(self, code: str, *, status: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"SignedEvidenceError({self.code!r})"
