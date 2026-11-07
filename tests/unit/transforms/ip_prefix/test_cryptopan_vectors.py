"""Crypto-PAn primitive: published vectors, keep-prefix composition and the inverse."""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest

from decoy_engine._opaque import OpaqueBox
from decoy_engine.transforms.network._cryptopan import CryptoPan

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "cryptopan"
_REFERENCE_KEY = bytes.fromhex(
    "15 22 17 8d 33 a4 cf 80 13 0a 5b 16 49 90 7d 10 d8 98 8f 83 79 79 65 27 62 57 4c 2d 2a 84 22 02"
)


def _third_column(name: str) -> list[str]:
    lines = (_FIXTURES / name).read_text(encoding="ascii").splitlines()
    return [line.split("\t")[2].strip() for line in lines]


def _reference_pairs() -> list[tuple[int, int]]:
    raw = _third_column("sample_trace_raw.dat")
    anon = _third_column("sample_trace_sanitized.dat")
    assert len(raw) == len(anon) == 100
    return [
        (int(ipaddress.IPv4Address(r)), int(ipaddress.IPv4Address(a)))
        for r, a in zip(raw, anon, strict=True)
    ]


def _cipher(key: bytes) -> CryptoPan:
    return CryptoPan(OpaqueBox(key))


def test_reference_trace_maps_exactly_and_inverts() -> None:
    cp = _cipher(_REFERENCE_KEY)
    for raw, anon in _reference_pairs():
        assert cp.encrypt(raw, 32, 0) == anon
        assert cp.decrypt(anon, 32, 0) == raw


@pytest.mark.parametrize(
    ("key", "address", "expected"),
    [
        (bytes(range(32)), "192.0.2.1", "2.90.93.17"),
        (bytes(range(32)), "2001:db8::1", "dd92:2c44:3fc0:ff1e:7ff9:c7f0:8180:7e00"),
        (b"32-char-str-for-AES-key-and-pad.", "192.0.2.1", "192.0.125.244"),
        (
            b"32-char-str-for-AES-key-and-pad.",
            "2001:db8::1",
            "27fe:8bc7:fee:1e:1e1f:f0fe:f0e1:83fd",
        ),
    ],
)
def test_yacryptopan_published_examples(key: bytes, address: str, expected: str) -> None:
    ip = ipaddress.ip_address(address)
    width = 32 if ip.version == 4 else 128
    out = _cipher(key).encrypt(int(ip), width, 0)
    rendered = str(ipaddress.IPv4Address(out) if width == 32 else ipaddress.IPv6Address(out))
    assert rendered == expected
    assert _cipher(key).decrypt(out, width, 0) == int(ip)


@pytest.mark.parametrize("keep", [8, 16, 24, 30])
def test_keep_prefix_is_the_reference_host_bits_under_the_verbatim_prefix(keep: int) -> None:
    cp = _cipher(_REFERENCE_KEY)
    host_mask = (1 << (32 - keep)) - 1
    for raw, anon in _reference_pairs():
        out = cp.encrypt(raw, 32, keep)
        assert out & ~host_mask & 0xFFFFFFFF == raw & ~host_mask & 0xFFFFFFFF
        assert out & host_mask == anon & host_mask
        assert cp.decrypt(out, 32, keep) == raw


def test_repr_never_shows_key_material() -> None:
    cp = _cipher(_REFERENCE_KEY)
    assert repr(cp) == "<CryptoPan>"
    assert not hasattr(cp, "__dict__")
