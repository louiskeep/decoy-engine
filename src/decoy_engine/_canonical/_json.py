"""Stub."""

from __future__ import annotations


class CanonicalJsonError(ValueError):
    """Stub."""

    def __init__(self, code: str = "", message: str = "") -> None:
        super().__init__(message)
        self.code = ""


def canonical_json_bytes(value: object) -> bytes:
    return b""


def seed_fingerprint_bytes(value: object) -> bytes:
    return b""
