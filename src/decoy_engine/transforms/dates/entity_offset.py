"""The ``date_shift`` ``entity_key`` option: one keyed day offset per entity.

Every date of one entity (a patient, a customer) moves by the same offset, so intervals and order
within the entity survive, in every column and table that shares the namespace and range:

    offset = min_days + int.from_bytes(
        derive(mask_key, namespace, entity_source(e))[:8], "big") % (max_days - min_days + 1)

Pattern: a keyed PRF of the entity identifier reduced into an inclusive day range, as Google Cloud
DLP's ``DateShiftConfig`` does with ``crypto_key`` + ``context`` ("the same shift for the same
context and crypto_key") and as MIMIC-IV assigns "a single date shift to each subject_id". The PRF
is the engine's HKDF-SHA256 then HMAC-SHA256 envelope (RFC 5869, RFC 2104;
``determinism/_derive.py``).

Domain separation: ``entity_source`` starts with byte ``0xFF``. Every ``_canonicalize_source``
output is one byte (bool), a 4-byte length starting ``0x00`` (int) or valid UTF-8, and ``0xFF``
never occurs in UTF-8, so no ``hash``, ``group_key``, ``fpe`` key or plain ``date_shift`` digest
can reproduce an entity offset. ``domain_separated_source`` is the reusable form of that rule.

Entity text is type-stable: integers, integral Decimals and their decimal strings agree, so an id
stored as int64 in one table and as text in another gets one offset.

The near-miss check uses the optimal-string-alignment form of the Damerau-Levenshtein distance
(Damerau 1964), so one transposition counts as one edit.

Messages and faults carry option names and type names only, never a cell value.
"""

from __future__ import annotations

import enum
import numbers
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from decoy_engine.kernel import canonicalize_derive_source
from decoy_engine.transforms.date_preserve import has_preserve_values

ENTITY_KEY_OPTION = "entity_key"
GROUP_BY_OPTION = "group_by"
# The largest day count pandas can add: `pd.Timedelta.max` is 106751 days and change.
MAX_ENTITY_OFFSET_DAYS = 106_751
DEFAULT_MIN_DAYS = -365
DEFAULT_MAX_DAYS = 365
ENTITY_OFFSET_TAG = b"date-shift-entity/v1"
_BOUND_OPTIONS = ("min_days", "max_days")
_NEAR_MISS_TARGET = "entitykey"


def domain_separated_source(tag: bytes, body: bytes) -> bytes:
    """``0xFF || tag || 0x00 || body``: a derive source no canonical cell value can equal.

    The ``0x00`` terminator keeps every tag prefix-free against every other tag.
    """
    if not tag or b"\x00" in tag:
        raise ValueError("a domain tag is non-empty and holds no NUL byte")
    return b"\xff" + tag + b"\x00" + body


class AnchorDomain(enum.Enum):
    """Which derivation the anchor column feeds."""

    # `group_by`: the anchor's canonical value in the plain date_shift domain.
    VALUE_KEYED_GROUP = "group_by"
    # `entity_key`: the domain-separated entity source.
    ENTITY = "entity_key"


@dataclass(frozen=True)
class Anchor:
    column: Any
    domain: AnchorDomain

    @property
    def option(self) -> str:
        return self.domain.value


class FaultKind(enum.Enum):
    KEY_INVALID = "key_invalid"
    BOUND_INVALID = "bound_invalid"
    WITH_GROUP_BY = "with_group_by"
    WITH_PRESERVE_VALUES = "with_preserve_values"


@dataclass(frozen=True)
class EntityKeyFault:
    kind: FaultKind
    option: str


@dataclass(frozen=True)
class EntityKeyConfig:
    column: str
    min_days: int
    max_days: int


@dataclass(frozen=True)
class UnsupportedEntityValue:
    """An entity cell of a type with no stable text form; carries the type name only."""

    type_name: str


def entity_key_present(provider_config: Mapping[str, Any]) -> bool:
    """True when ``entity_key`` is set to anything but None, valid or not."""
    return provider_config.get(ENTITY_KEY_OPTION) is not None


def _bound(raw: Any, default: int) -> int | None:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if abs(raw) > MAX_ENTITY_OFFSET_DAYS:
        return None
    return raw


def parse_entity_key(
    provider_config: Mapping[str, Any],
) -> EntityKeyConfig | EntityKeyFault | None:
    """The typed ``entity_key`` config, a fault, or None when the option is not set.

    Single-column rules only; column existence and cross-column consistency are compile checks.
    """
    if not entity_key_present(provider_config):
        return None
    column = provider_config[ENTITY_KEY_OPTION]
    if not isinstance(column, str) or not column.strip():
        return EntityKeyFault(FaultKind.KEY_INVALID, ENTITY_KEY_OPTION)
    if provider_config.get(GROUP_BY_OPTION):
        return EntityKeyFault(FaultKind.WITH_GROUP_BY, GROUP_BY_OPTION)
    if has_preserve_values(provider_config):
        return EntityKeyFault(FaultKind.WITH_PRESERVE_VALUES, "preserve_values")
    lo = _bound(provider_config.get("min_days"), DEFAULT_MIN_DAYS)
    if lo is None:
        return EntityKeyFault(FaultKind.BOUND_INVALID, "min_days")
    hi = _bound(provider_config.get("max_days"), DEFAULT_MAX_DAYS)
    if hi is None:
        return EntityKeyFault(FaultKind.BOUND_INVALID, "max_days")
    if lo > hi:
        lo, hi = hi, lo
    return EntityKeyConfig(column=column, min_days=lo, max_days=hi)


def date_shift_anchor(provider_config: Mapping[str, Any]) -> Anchor | None:
    """The sibling column a ``date_shift`` column anchors on, and its derivation domain.

    A valid ``entity_key`` wins; otherwise a truthy ``group_by`` (validated by the compile check,
    so it is returned as configured). An invalid ``entity_key`` yields no entity anchor: the
    handler refuses it and the native gate declines it on presence alone.
    """
    entity = parse_entity_key(provider_config)
    if isinstance(entity, EntityKeyConfig):
        return Anchor(entity.column, AnchorDomain.ENTITY)
    group_by = provider_config.get(GROUP_BY_OPTION)
    if group_by:
        return Anchor(group_by, AnchorDomain.VALUE_KEYED_GROUP)
    return None


def entity_column(entry: Mapping[str, Any]) -> str | None:
    """The entity column a ``date_shift`` column entry reads, for its column-access declaration."""
    pc = entry.get("provider_config")
    if not isinstance(pc, Mapping):
        return None
    entity = parse_entity_key(pc)
    return entity.column if isinstance(entity, EntityKeyConfig) else None


def _osa_distance(a: str, b: str) -> int:
    """Optimal-string-alignment (restricted Damerau-Levenshtein) edit distance."""
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[len(b)]


def near_miss_entity_keys(provider_config: Mapping[str, Any]) -> tuple[str, ...]:
    """Keys other than ``entity_key`` that are one edit from it after normalisation.

    Normalised: lowercase, ``_`` and ``-`` removed. Such a key can only be a typo of the option,
    and ignoring it would silently fall back to per-value shifting.
    """
    found = []
    for key in provider_config:
        if not isinstance(key, str) or key == ENTITY_KEY_OPTION:
            continue
        norm = key.lower().replace("_", "").replace("-", "")
        if _osa_distance(norm, _NEAR_MISS_TARGET) <= 1:
            found.append(key)
    return tuple(found)


def _entity_text(value: Any) -> bytes | None | UnsupportedEntityValue:
    if isinstance(value, Decimal):
        if not value.is_finite():
            return UnsupportedEntityValue("Decimal")
        if value == value.to_integral_value():
            return str(int(value)).encode("ascii")
        return str(value).encode("utf-8")
    if value is None or (not isinstance(value, (str, bytes)) and _is_missing(value)):
        return None
    if isinstance(value, bool) or type(value).__name__ == "bool_":
        return UnsupportedEntityValue(type(value).__name__)
    if isinstance(value, numbers.Integral):
        return str(int(value)).encode("ascii")
    if isinstance(value, str):
        return canonicalize_derive_source(value) if value.strip() else None
    if isinstance(value, uuid.UUID):
        return str(value).encode("ascii")
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat().encode("ascii")
    return UnsupportedEntityValue(type(value).__name__)


def _is_missing(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def entity_source(value: Any) -> bytes | None | UnsupportedEntityValue:
    """The derive source for one entity cell; None when the row has no entity.

    No entity: null, NaN, NA, NaT, ``""`` and blank strings (fixed-width CHAR padding).
    """
    text = _entity_text(value)
    if text is None or isinstance(text, UnsupportedEntityValue):
        return text
    return domain_separated_source(ENTITY_OFFSET_TAG, text)


__all__ = [
    "DEFAULT_MAX_DAYS",
    "DEFAULT_MIN_DAYS",
    "ENTITY_KEY_OPTION",
    "ENTITY_OFFSET_TAG",
    "GROUP_BY_OPTION",
    "MAX_ENTITY_OFFSET_DAYS",
    "Anchor",
    "AnchorDomain",
    "EntityKeyConfig",
    "EntityKeyFault",
    "FaultKind",
    "UnsupportedEntityValue",
    "date_shift_anchor",
    "domain_separated_source",
    "entity_column",
    "entity_key_present",
    "entity_source",
    "near_miss_entity_keys",
    "parse_entity_key",
]
