"""ip_prefix: keep an IP address's first N bits and pseudonymise the rest with Crypto-PAn.

An IPv4 address (dotted quad, or the IPv4-mapped spelling ``::ffff:a.b.c.d``) keeps its first
``keep_bits_v4`` bits; any other IPv6 address keeps its first ``keep_bits_v6`` bits. The bits after
that go through Crypto-PAn (``_cryptopan.py``), so within each class the mapping is a keyed
permutation that is prefix-preserving below the kept prefix. ``keep_bits`` 0 is plain Crypto-PAn.

Mapped addresses take the IPv4 rule, so a column mixing ``1.2.3.4`` and ``::ffff:1.2.3.4`` still
joins; their output is built as a string because ``ipaddress`` renders mapped addresses
differently across CPython versions. That leaves one /96 block ``Q`` of IPv6 inputs whose plain
Crypto-PAn image is the mapped block ``M`` (``::ffff:0:0/96``), where mapped outputs live, while
``M``'s own image block ``P`` is unused. Moving ``Q``'s outputs from ``M`` to ``P`` keeps the IPv6
mapping a bijection of IPv6-outside-``M`` (inputs in ``Q`` lose prefix preservation against other
IPv6 inputs, nothing else changes).

Config is parsed once, here, for the compile check, the strategy handler and unmask: a fault is
returned as a typed value each caller maps to its own reason code. Malformed cells (not a
``str``, rejected by ``ipaddress``, a zone ID, a CIDR value) are reported as ``None``, never
normalised: the handler records them as row errors.
"""

from __future__ import annotations

import enum
import ipaddress
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from decoy_engine import reasons
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.network._cryptopan import CryptoPan

STRATEGY: Final[str] = "ip_prefix"
KEEP_BITS_V4: Final[str] = "keep_bits_v4"
KEEP_BITS_V6: Final[str] = "keep_bits_v6"
DEFAULT_KEEP_BITS_V4: Final[int] = 24
DEFAULT_KEEP_BITS_V6: Final[int] = 48
# At least two keyed host bits: with one, every output is its input or that input's sibling.
MAX_KEEP_BITS_V4: Final[int] = 30
MAX_KEEP_BITS_V6: Final[int] = 126
_LIMITS: Final[dict[str, tuple[int, int]]] = {
    KEEP_BITS_V4: (DEFAULT_KEEP_BITS_V4, MAX_KEEP_BITS_V4),
    KEEP_BITS_V6: (DEFAULT_KEEP_BITS_V6, MAX_KEEP_BITS_V6),
}

_MAPPED_BLOCK: Final[int] = 0xFFFF  # top 96 bits of ::ffff:0:0/96
_MAPPED_TEXT_PREFIX: Final[str] = "::ffff:"
_LOW_32: Final[int] = 0xFFFFFFFF


class FaultKind(enum.Enum):
    UNKNOWN_OPTION = "unknown_option"
    KEEP_BITS_INVALID = "keep_bits_invalid"
    KEEPS_TOO_MANY_BITS = "keeps_too_many_bits"


@dataclass(frozen=True)
class IpPrefixConfigFault:
    kind: FaultKind
    option: str

    def describe(self) -> str:
        if self.kind is FaultKind.UNKNOWN_OPTION:
            return f"unknown option {self.option!r}; the options are {sorted(_LIMITS)}"
        _, cap = _LIMITS[self.option]
        return f"{self.option!r} must be an integer from 0 to {cap}"


@dataclass(frozen=True)
class IpPrefixConfig:
    keep_bits_v4: int = DEFAULT_KEEP_BITS_V4
    keep_bits_v6: int = DEFAULT_KEEP_BITS_V6


_PLAN_CODE: Final[dict[FaultKind, str]] = {
    FaultKind.UNKNOWN_OPTION: reasons.plan.IP_PREFIX_UNKNOWN_OPTION,
    FaultKind.KEEP_BITS_INVALID: reasons.plan.IP_PREFIX_KEEP_BITS_INVALID,
    FaultKind.KEEPS_TOO_MANY_BITS: reasons.plan.IP_PREFIX_KEEPS_TOO_MANY_BITS,
}


def parse_ip_prefix_config(
    provider_config: Mapping[str, Any] | None,
) -> IpPrefixConfig | IpPrefixConfigFault:
    """The typed config, or the first fault in it (unknown keys first, then each option)."""
    given = dict(provider_config or {})
    for name in sorted(given):
        if name not in _LIMITS:
            return IpPrefixConfigFault(FaultKind.UNKNOWN_OPTION, name)
    resolved: dict[str, int] = {}
    for name, (default, cap) in _LIMITS.items():
        value = given.get(name, default)
        # bool is an int subclass; a True here is a config error, not 1.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return IpPrefixConfigFault(FaultKind.KEEP_BITS_INVALID, name)
        if value > cap:
            return IpPrefixConfigFault(FaultKind.KEEPS_TOO_MANY_BITS, name)
        resolved[name] = value
    return IpPrefixConfig(resolved[KEEP_BITS_V4], resolved[KEEP_BITS_V6])


def _parse_address(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.scope_id is not None:
        return None
    return address


def host_identity(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The host ``value`` names, as ``mask`` maps it (IPv4-mapped is its IPv4), or None."""
    address = _parse_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def canonical_text(value: object) -> str | None:
    """The canonical spelling ``mask`` and ``unmask`` emit for ``value``, or None if malformed."""
    address = _parse_address(value)
    if address is None:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _MAPPED_TEXT_PREFIX + str(address.ipv4_mapped)
    return str(address)


class IpPrefixMasker:
    """Masks and unmasks single values under one key and one config."""

    __slots__ = ("_cipher", "_image_of_mapped", "_keep_v4", "_keep_v6")

    def __init__(self, cipher: CryptoPan, config: IpPrefixConfig) -> None:
        self._cipher = cipher
        self._keep_v4 = config.keep_bits_v4
        self._keep_v6 = config.keep_bits_v6
        # P: the top 96 bits of M's image. Prefix preservation means they depend only on M's
        # top 96 bits, so one encryption gives the whole block.
        self._image_of_mapped = cipher.encrypt(_MAPPED_BLOCK << 32, 128, self._keep_v6) >> 32

    def __repr__(self) -> str:
        return "<IpPrefixMasker>"

    def _v4(self, op: Callable[[int, int, int], int], value: int) -> str:
        return str(ipaddress.IPv4Address(op(value, 32, self._keep_v4)))

    def mask(self, value: object) -> str | None:
        address = _parse_address(value)
        if address is None:
            return None
        if isinstance(address, ipaddress.IPv4Address):
            return self._v4(self._cipher.encrypt, int(address))
        if address.ipv4_mapped is not None:
            return _MAPPED_TEXT_PREFIX + self._v4(self._cipher.encrypt, int(address.ipv4_mapped))
        out = self._cipher.encrypt(int(address), 128, self._keep_v6)
        if out >> 32 == _MAPPED_BLOCK:
            out = (self._image_of_mapped << 32) | (out & _LOW_32)
        return str(ipaddress.IPv6Address(out))

    def unmask(self, value: object) -> str | None:
        address = _parse_address(value)
        if address is None:
            return None
        if isinstance(address, ipaddress.IPv4Address):
            return self._v4(self._cipher.decrypt, int(address))
        if address.ipv4_mapped is not None:
            return _MAPPED_TEXT_PREFIX + self._v4(self._cipher.decrypt, int(address.ipv4_mapped))
        out = int(address)
        # Block P holds the swapped outputs of Q; when Q is M itself, P is M and mapped
        # outputs have already returned above.
        if out >> 32 == self._image_of_mapped:
            out = (_MAPPED_BLOCK << 32) | (out & _LOW_32)
        return str(ipaddress.IPv6Address(self._cipher.decrypt(out, 128, self._keep_v6)))


def check_ip_prefix_config(config: Mapping[str, Any]) -> None:
    """Refuse an ip_prefix column the handler could not honour, at plan compile.

    Per column: unknown options, invalid or too-large ``keep_bits_*``, and use as a ``nested``
    child. Across columns: two ip_prefix columns in one namespace share one key, so they must
    resolve to the same keep bits or equal addresses would mask differently and joins break.
    Config-only and non-mutating.
    """
    tables = config.get("tables") if isinstance(config.get("tables"), list) else []
    by_namespace: dict[str, tuple[str, IpPrefixConfig]] = {}
    for table_entry in tables or ():
        if not isinstance(table_entry, dict):
            continue
        table = table_entry.get("name", "?")
        for col in table_entry.get("columns") or ():
            if not isinstance(col, dict):
                continue
            column = col.get("name", "?")
            path = f"tables.{table}.columns.{column}"
            pc = col.get("provider_config")
            pc = pc if isinstance(pc, dict) else {}
            if col.get("strategy") == "nested" and pc.get("strategy") == STRATEGY:
                raise PlanCompileError(
                    code=reasons.plan.IP_PREFIX_UNSUPPORTED_IN_NESTED,
                    path=f"{path}.provider_config.strategy",
                    message=(
                        f"column {column!r} in table {table!r} uses ip_prefix as a nested "
                        "child, which is not supported; mask the address at top level."
                    ),
                )
            if col.get("strategy") != STRATEGY:
                continue
            parsed = parse_ip_prefix_config(pc)
            if isinstance(parsed, IpPrefixConfigFault):
                raise PlanCompileError(
                    code=_PLAN_CODE[parsed.kind],
                    path=f"{path}.provider_config.{parsed.option}",
                    message=f"ip_prefix column {column!r} in table {table!r}: {parsed.describe()}.",
                )
            # The column's own namespace, as the handler reads it; a column without one is
            # refused at run (ip_prefix_requires_namespace).
            namespace = col.get("namespace")
            if not isinstance(namespace, str) or not namespace:
                continue
            first = by_namespace.setdefault(namespace, (f"{table}.{column}", parsed))
            if first[1] != parsed:
                raise PlanCompileError(
                    code=reasons.plan.IP_PREFIX_NAMESPACE_KEEP_BITS_MISMATCH,
                    path=f"{path}.provider_config",
                    message=(
                        f"ip_prefix columns {first[0]} and {table}.{column} share namespace "
                        f"{namespace!r} but keep different prefix lengths (keep_bits_v4 "
                        f"{first[1].keep_bits_v4} vs {parsed.keep_bits_v4}, keep_bits_v6 "
                        f"{first[1].keep_bits_v6} vs {parsed.keep_bits_v6}); one namespace is one "
                        "mapping, so give them the same keep bits or different namespaces."
                    ),
                )
