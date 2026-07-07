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
    if isinstance(value, numbers.Number):
        # Non-Integral, non-float numeric types (decimal.Decimal is the
        # reachable one; it registers as numbers.Number but not
        # numbers.Integral/Real) that Python's own == / hash already fold
        # onto int above: hash(Decimal("1")) == hash(1) and Decimal("1") == 1
        # (the PEP 3141 numeric tower), which is exactly the equality a plain
        # dict-keyed parent_map (the pandas adapter's _parent_map) relies on
        # without ever calling this function's int/float branches. Collapse
        # the whole-valued case the same way the float branch above does, so
        # the join-key encoder's token matches; a NaN-valued Number collapses
        # to NULL_FK_KEY the same way float NaN does (pandas' `pd.isna`
        # treats both as missing). A genuinely fractional value (e.g.
        # Decimal("2.5")) is returned unchanged rather than guessed at: it
        # stays distinct from any int it is not equal to, so this never
        # collapses two values full-frame would keep apart.
        try:
            is_nan = value != value  # only NaN is unequal to itself
        except Exception:
            return value
        if is_nan:
            return NULL_FK_KEY
        try:
            # numbers.Number does not declare __int__/__trunc__ in the
            # typeshed stubs (only its Integral/Real/Rational subtypes do),
            # so the static type is widened to Any here; the runtime
            # TypeError branch below still catches a Number that has no
            # such conversion (e.g. complex).
            as_int = int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError, OverflowError):
            return value
        return as_int if value == as_int else value
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
