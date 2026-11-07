"""Shared card-number builders for the keep_bin tests (deterministic, no I/O)."""

from __future__ import annotations

import random

from decoy_engine import checksums
from decoy_engine.transforms.fpe import FF1_TWEAK_SCOPE_COLUMN, build_ff1_tweak

KEY = bytes(range(32))
DIGITS = "0123456789"
TWEAK = build_ff1_tweak(FF1_TWEAK_SCOPE_COLUMN, "card")
# Real-shaped issuer prefixes (Visa, Mastercard 2-series, Amex, Discover, UnionPay, JCB).
BINS = ("453957", "222100", "378282", "601100", "622126", "353011")


def luhn(body: str) -> str:
    return body + checksums.calc_check_digit("luhn", body)


def pan(rng: random.Random, length: int, prefix: str | None = None) -> str:
    """A Luhn-valid PAN of `length` digits starting with `prefix` (random if None)."""
    head = prefix if prefix is not None else rng.choice(BINS)
    body = head + "".join(str(rng.randint(0, 9)) for _ in range(length - 1 - len(head)))
    return luhn(body)


def digits_of(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())
