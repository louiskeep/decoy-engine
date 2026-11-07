"""Self-maps of the reference Crypto-PAn construction, and the checks that must tolerate them.

The reference pads each prefix with the key's pad bits, so a child whose next bit equals the pad
bit inherits its parent's flip. A host with h keyed bits maps to itself with probability
1/2 * (3/4)**(h - 1).
"""

from __future__ import annotations

import random

import pandas as pd

from decoy_engine._opaque import OpaqueBox
from decoy_engine.transforms.network._cryptopan import CryptoPan
from decoy_engine.transforms.network.ip_prefix import IpPrefixConfig, IpPrefixMasker
from decoy_engine.verification.postmask.residual_pii import check_residual_pii

_BLOCKS = 400


def _fixed_hosts(cp: CryptoPan, base: int, keep: int) -> int:
    hosts = 1 << (32 - keep)
    return sum(cp.encrypt(base + x, 32, keep) == base + x for x in range(hosts))


def test_keep_24_self_map_rate_matches_the_reference_construction() -> None:
    rng = random.Random(20261107)
    fixed = []
    for _ in range(_BLOCKS):
        cp = CryptoPan(OpaqueBox(rng.randbytes(32)))
        fixed.append(_fixed_hosts(cp, rng.getrandbits(24) << 8, 24))
    rate = sum(fixed) / (_BLOCKS * 256)
    assert abs(rate - 0.5 * 0.75**7) < 0.015  # 1/2 * (3/4)**7 ~= 0.0667
    assert 0.4 < sum(f == 0 for f in fixed) / _BLOCKS < 0.6  # half the blocks have none
    assert all(f % 2 == 0 for f in fixed)  # self-maps come in pairs


def test_the_pad_host_maps_to_itself_or_its_complement() -> None:
    rng = random.Random(7)
    for keep in (16, 24, 28):
        host_bits = 32 - keep
        for _ in range(50):
            cp = CryptoPan(OpaqueBox(rng.randbytes(32)))
            # The pad's bits at the host positions of a left-aligned 32-bit address.
            pad_host = (cp._pad >> (128 - 32)) & ((1 << host_bits) - 1)
            base = rng.getrandbits(keep) << host_bits
            out = cp.encrypt(base | pad_host, 32, keep) & ((1 << host_bits) - 1)
            assert out in (pad_host, pad_host ^ ((1 << host_bits) - 1))


def _slash30_with(fixed_count: int) -> tuple[list[str], list[str]]:
    hosts = [f"198.51.100.{i}" for i in range(4)]
    for i in range(256):
        masker = IpPrefixMasker(CryptoPan(OpaqueBox(bytes([i]) * 32)), IpPrefixConfig(30, 48))
        out = [masker.mask(h) for h in hosts]
        if sum(a == b for a, b in zip(hosts, out, strict=True)) == fixed_count:
            return hosts, [o for o in out if o is not None]
    raise AssertionError(f"no key fixes exactly {fixed_count} hosts")


_CFG = {
    "version": 2,
    "tables": [
        {
            "name": "t",
            "columns": [
                {
                    "name": "ip",
                    "strategy": "ip_prefix",
                    "namespace": "n",
                    "provider_config": {"keep_bits_v4": 30},
                }
            ],
        }
    ],
}


def _residual(src: list[str], out: list[str]) -> list[tuple[str, float | None]]:
    findings = check_residual_pii(
        {"t": pd.DataFrame({"ip": out})}, _CFG, source_frames={"t": pd.DataFrame({"ip": src})}
    )
    return [(f.severity, f.source_identity_rate) for f in findings]


def test_correct_output_with_half_its_rows_self_mapped_does_not_fail_residual_pii() -> None:
    # One /30 at keep 30: a key that fixes a pair gives 50% positional identity, which the
    # substitution thresholds would call a failed mask.
    src, out = _slash30_with(2)
    assert _residual(src, out) == [("info", 0.5)]


def test_a_column_whose_every_row_is_self_mapped_still_fails_residual_pii() -> None:
    # The value-reuse rule fails on full positional identity: indistinguishable from a mask
    # that did not fire. For a lone /30 at keep 30 that is one key in four (known limit).
    src, out = _slash30_with(4)
    assert [s for s, _ in _residual(src, out)] == ["fail"]
