"""Seed helpers (stub: the checks land with the implementation)."""

from __future__ import annotations


def seed_bytes_to_int(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


def fresh_seed() -> bytes:
    return b"\x00" * 8


def resolve_seed(supplied: bytes | None) -> bytes:
    return supplied if supplied is not None else fresh_seed()
