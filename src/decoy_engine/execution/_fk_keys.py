"""Canonical FK match keys.

These helpers define equality for relationship joins. They are deliberately
separate from derive-input canonicalization, whose output bytes are part of the
seed protocol and reject floats.
"""

from __future__ import annotations

import math
import numbers
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class _NullFkKey:
    pass


NULL_FK_KEY: Final = _NullFkKey()


def fk_key_value(value: object) -> object:
    """Normalize one FK key component to match pandas parent-map semantics."""
    if value is None:
        return NULL_FK_KEY
    if isinstance(value, bool):
        # Accepted divergence: pandas's parent_map dict collides True with 1
        # (hash(True) == hash(1)) while fk_join_key tags them distinctly
        # (BOOL:1 vs INT:1). Only reachable for bool-typed FK keys, which is
        # pathological, and a masked bool vs. int token differs anyway.
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return NULL_FK_KEY
        return int(value) if value.is_integer() else value
    return value


def fk_join_key(value: object) -> str:
    """Encode one normalized FK key component for relational joins."""
    normalized = fk_key_value(value)
    if normalized is NULL_FK_KEY:
        return "\x00NULL"
    if isinstance(normalized, bool):
        return f"\x00BOOL:{int(normalized)}"
    if isinstance(normalized, int):
        return f"\x00INT:{normalized}"
    if isinstance(normalized, float):
        return f"\x00FLOAT:{normalized!r}"
    if isinstance(normalized, str):
        return f"\x00STR:{len(normalized)}:{normalized}"
    return f"\x00OBJ:{type(normalized).__qualname__}:{normalized!r}"


def fk_join_key_tuple(values: tuple[object, ...]) -> str:
    """Encode a full FK key tuple for relational joins.

    Same injective length-prefixed framing idea as `_encode_int`'s ASN.1 DER
    (X.690 §8.3) length-prefix lineage (kernel/_canonicalize.py): prefixing
    each component with its own length keeps two differently-shaped key
    tuples from concatenating into the same joined string.
    """
    parts = [fk_join_key(value) for value in values]
    return "".join(f"{len(part)}:{part}" for part in parts)


__all__ = ["NULL_FK_KEY", "fk_join_key", "fk_join_key_tuple", "fk_key_value"]
