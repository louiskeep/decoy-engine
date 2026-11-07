"""P1 to P4 of the ip_prefix mapping (plan section 1), by property test and exhaustive checks."""

from __future__ import annotations

import ipaddress

from hypothesis import HealthCheck, event, given, seed, settings
from hypothesis import strategies as st

from decoy_engine._opaque import OpaqueBox
from decoy_engine.transforms.network._cryptopan import CryptoPan
from decoy_engine.transforms.network.ip_prefix import (
    IpPrefixConfig,
    IpPrefixMasker,
    canonical_text,
)

_MAPPED_BLOCK = 0xFFFF  # top 96 bits of ::ffff:0:0/96
_FIXED_KEY = bytes(range(100, 132))


def _masker(key: bytes, keep_v4: int, keep_v6: int) -> IpPrefixMasker:
    return IpPrefixMasker(CryptoPan(OpaqueBox(key)), IpPrefixConfig(keep_v4, keep_v6))


def _swapped_block(key: bytes, keep_v6: int) -> int:
    """Q: the /96 whose plain Crypto-PAn image is the mapped block."""
    return CryptoPan(OpaqueBox(key)).decrypt(_MAPPED_BLOCK << 32, 128, keep_v6) >> 32


def _mapped(text: str) -> ipaddress.IPv4Address | None:
    ip = ipaddress.ip_address(text)
    return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None


def _class_of(text: str) -> str:
    return "v4" if ipaddress.ip_address(text).version == 4 or _mapped(text) else "v6"


def _bits(text: str) -> tuple[int, int]:
    """(value, width) of the address the class's rule maps: the embedded IPv4 for mapped."""
    ip = ipaddress.ip_address(text)
    if ip.version == 4:
        return int(ip), 32
    if _mapped(text) is not None:
        return int(_mapped(text)), 32
    return int(ip), 128


def _common_prefix(a: int, b: int, width: int) -> int:
    diff = a ^ b
    return width if diff == 0 else width - diff.bit_length()


def _in_block(text: str, block: int) -> bool:
    ip = ipaddress.ip_address(text)
    return ip.version == 6 and _mapped(text) is None and int(ip) >> 32 == block


_v4 = st.integers(0, 2**32 - 1).map(lambda n: str(ipaddress.IPv4Address(n)))
_mapped_text = st.integers(0, 2**32 - 1).map(lambda n: "::ffff:" + str(ipaddress.IPv4Address(n)))
_v6 = st.integers(0, 2**128 - 1).map(lambda n: str(ipaddress.IPv6Address(n)))
_v6_low = st.integers(0, 2**48 - 1).map(lambda n: str(ipaddress.IPv6Address(n)))  # inside ::/80
_address = st.one_of(_v4, _mapped_text, _v6, _v6_low)


def _sibling(text: str, shared: int) -> str:
    """An address of the same class sharing at least `shared` leading bits with `text`."""
    value, width = _bits(text)
    shared = min(shared, width)
    flip = 1 << (width - 1 - shared) if shared < width else 0
    other = value ^ flip
    ip = ipaddress.ip_address(text)
    if width == 32:
        rendered = str(ipaddress.IPv4Address(other))
        return "::ffff:" + rendered if ip.version == 6 else rendered
    if other >> 32 == _MAPPED_BLOCK:
        other ^= 1 << 40  # stay out of the mapped block
    return str(ipaddress.IPv6Address(other))


@seed(20261107)
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    key=st.binary(min_size=32, max_size=32),
    keep_v4=st.integers(0, 30),
    keep_v6=st.integers(0, 126),
    a=_address,
    shared=st.integers(0, 128),
)
def test_p1_to_p4_and_round_trip(
    key: bytes, keep_v4: int, keep_v6: int, a: str, shared: int
) -> None:
    m = _masker(key, keep_v4, keep_v6)
    b = _sibling(a, shared)
    q_block = _swapped_block(key, keep_v6)
    for x in (a, b):
        out = m.mask(x)
        assert out is not None
        assert _class_of(out) == _class_of(x)  # class preserved
        xv, width = _bits(x)
        ov, _ = _bits(out)
        keep = keep_v4 if width == 32 else keep_v6
        assert _common_prefix(xv, ov, width) >= keep  # P1
        assert m.unmask(out) == canonical_text(x)  # P3 by the inverse
        if _mapped(x) is not None:  # P4
            assert out == "::ffff:" + m.mask(str(_mapped(x)))
    # P2 holds for same-class pairs with neither or both inputs in the swapped block; the
    # generator only builds same-class pairs, so the swap split is the one exclusion.
    assert _class_of(a) == _class_of(b)
    if _in_block(a, q_block) != _in_block(b, q_block):
        event("pair split by the swapped block (no P2 claim)")
        return
    av, width = _bits(a)
    bv, _ = _bits(b)
    oa, _ = _bits(m.mask(a))
    ob, _ = _bits(m.mask(b))
    assert _common_prefix(oa, ob, width) == _common_prefix(av, bv, width)


def test_exhaustive_v4_slash24_and_slash20_are_permutations_inside_the_prefix() -> None:
    for keep, base in ((24, 0xC0000200), (20, 0x0A141000)):
        m = _masker(_FIXED_KEY, keep, 48)
        hosts = 1 << (32 - keep)
        outs = {m.mask(str(ipaddress.IPv4Address(base + h))) for h in range(hosts)}
        assert len(outs) == hosts
        net = ipaddress.ip_network(f"{ipaddress.IPv4Address(base)}/{keep}")
        assert all(ipaddress.ip_address(o) in net for o in outs)


def test_exhaustive_v6_slash112_is_a_permutation() -> None:
    m = _masker(_FIXED_KEY, 24, 112)
    base = int(ipaddress.IPv6Address("2001:db8:1:2:3:4:5:0"))
    outs = {m.mask(str(ipaddress.IPv6Address(base + h))) for h in range(1 << 16)}
    assert len(outs) == 1 << 16
    net = ipaddress.ip_network("2001:db8:1:2:3:4:5:0/112")
    assert all(ipaddress.ip_address(o) in net for o in outs)


def test_swapped_block_never_collides_with_its_mapped_partner() -> None:
    """Q's plain Crypto-PAn image lands in ::ffff:0:0/96; the swap must move it.

    For q in Q, the unswapped output would be ::ffff:<low 32 bits of g(q)>. Its mapped partner is
    ::ffff: + the IPv4-rule inverse of those low 32 bits, whose output is exactly that address.
    Without the swap the two outputs are the same address.
    """
    keep_v4, keep_v6 = 24, 80
    key = _FIXED_KEY
    cipher = CryptoPan(OpaqueBox(key))
    m = _masker(key, keep_v4, keep_v6)
    q_block = _swapped_block(key, keep_v6)
    assert q_block != _MAPPED_BLOCK  # this key moves a block (probability 1 - 2**-16)
    seen: set[ipaddress.IPv6Address] = set()
    for low in range(0, 2**32, 2**32 // 4096):
        q = (q_block << 32) | low
        g_low = cipher.encrypt(q, 128, keep_v6) & 0xFFFFFFFF
        partner = "::ffff:" + str(ipaddress.IPv4Address(cipher.decrypt(g_low, 32, keep_v4)))
        out_q = ipaddress.ip_address(m.mask(str(ipaddress.IPv6Address(q))))
        out_partner = ipaddress.ip_address(m.mask(partner))
        assert out_q != out_partner
        assert _mapped(str(out_q)) is None
        seen.update((out_q, out_partner))
        assert m.unmask(str(out_q)) == str(ipaddress.IPv6Address(q))
        assert m.unmask(m.mask(partner)) == partner
    assert len(seen) == 2 * 4096


def test_mapped_output_is_string_built_on_every_python() -> None:
    m = _masker(_FIXED_KEY, 24, 48)
    out = m.mask("::FFFF:192.0.2.7")
    assert out == "::ffff:" + m.mask("192.0.2.7")
    assert out.startswith("::ffff:") and out.count(".") == 3
