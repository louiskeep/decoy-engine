"""Exception hierarchy for the identifiers package.

`IdentifierError` covers identifier-generation failures (blocklist
exhaustion, validation rejection). `IdentifierFormatError` is the
narrower subclass for format-validation specifically.

Both are peers of `ProviderError` rather than subclasses: identifier
generation can fail at runtime in ways that don't fit the provider-
routing model (e.g. all 4 HMAC offsets hit the blocklist).
"""

from __future__ import annotations


class IdentifierError(Exception):
    """Runtime failure inside an identifier adapter or domain.

    Codes used in S6:

    - `blocklist_exhausted`: all 4 derive() rehash offsets produced
      blocklisted output (practically unreachable; ~1.5e-4 for SSN).
    - `validator_rejected`: a generated value failed its own validator
      (defensive guard; suggests adapter bug).
    """

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


class IdentifierFormatError(IdentifierError):
    """Format-validation failure narrower than IdentifierError."""
