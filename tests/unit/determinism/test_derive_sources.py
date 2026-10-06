"""`DeriveContext.derive_sources` equals per-source `derive_source`, byte for byte."""

from __future__ import annotations

import pytest

from decoy_engine.determinism._derive import DeriveContext, derive

_SOURCES = [b"", b"a", b"\x00" * 3, "café".encode(), b"x" * 10_000, b"\xff\xfe"]


@pytest.mark.parametrize("seed", [b"\x00" * 8, b"\x01" * 32], ids=["job_seed", "mask_key"])
@pytest.mark.parametrize("namespace", ["ns", "faker-nd/1:t/1:c", "naïve/日本"])
def test_derive_sources_matches_derive_source_and_derive(seed: bytes, namespace: str) -> None:
    ctx = DeriveContext.for_column(seed, namespace)
    batched = list(ctx.derive_sources(namespace, _SOURCES))
    assert batched == [ctx.derive_source(namespace, s) for s in _SOURCES]
    assert batched == [derive(seed, namespace, s) for s in _SOURCES]


def test_derive_sources_of_nothing_is_empty() -> None:
    ctx = DeriveContext.for_column(b"\x00" * 8, "ns")
    assert list(ctx.derive_sources("ns", [])) == []
