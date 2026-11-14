"""Canonical FK match keys: the one definition of when two FK key values are the same key.

Every FK join route (pandas full-frame, chunked, out-of-core, mixed bridge) matches through
these tokens, and the post-scan gate matches mixed-type key ends through them too, so the gate
and the join cannot disagree. It sits in `internal` (pandas and the standard library only), the
tier profiling and execution both import from, so profiling needs no back import. They are deliberately separate from
derive-input canonicalization, whose output bytes are part of the seed protocol and reject
floats. `decoy_engine.execution._fk_keys` re-exports every name here.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import numbers
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import pandas as pd


@dataclass(frozen=True)
class _NullFkKey:
    pass


NULL_FK_KEY: Final = _NullFkKey()


def fk_key_value(value: object) -> object:
    """Normalize one FK key component to match pandas parent-map semantics."""
    if value is None:
        return NULL_FK_KEY
    if isinstance(value, bool):
        # The oracle's parent_map is a plain Python dict; `True`/`False` hash
        # and compare equal to `1`/`0` (bool is an int subtype), so a bool
        # parent key and a 0/1 int child key collide there for free. This
        # normalization makes that same collision reachable through
        # `fk_join_key`'s string encoding (which cannot rely on Python's
        # object equality), so a bool parent key and a 0/1 int child key mint
        # the identical join token the oracle's dict already produces.
        return int(value)
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


# Explicit, wide-precision context for `_decimal_join_token`'s `.normalize()`
# call: normalize() rounds using its context's precision, and the *ambient*
# default context caps at 28 significant digits. Arrow's decimal128/decimal256
# key types go up to 38/76 significant digits, so normalizing under the
# default context could silently round a legitimate high-precision key -- a
# correctness bug worse than the one being fixed. 200 digits is comfortably
# above decimal256's ceiling with headroom, so for a Decimal-typed key
# normalize() only ever strips a trailing-zero exponent, never rounds.
#
# The RI fix (float branch of `fk_join_key`) also routes floats here via
# `Decimal(a_float)`, whose EXACT expansion CAN exceed 200 digits (a subnormal
# like `Decimal(5e-324)` is ~751 digits and IS rounded to 200 here). That does
# not break the fold's injectivity: two distinct IEEE doubles differ within
# ~17 significant decimal digits, far inside 200, so their rounded tokens stay
# distinct; and an equal float/Decimal pair has a short exact expansion that
# never rounds. So a float can round here without ever colliding with a
# different key -- rounding only trims digits both members already share.
_DECIMAL_JOIN_CONTEXT: Final = decimal.Context(prec=200)


def _decimal_join_token(value: Decimal) -> str:
    """Canonical join-token text for a Decimal, matching the oracle's dict.

    `Decimal('1.20') == Decimal('1.2')` and they hash equal -- the decimal
    module deliberately satisfies the hash/eq contract across exponent-only
    (trailing-zero) differences -- so the pandas oracle's plain-dict
    `parent_map` already resolves a parent masked under one scale and a child
    read under another as the SAME key. `repr()` does not: it renders the
    coefficient and exponent verbatim, so those two equal Decimals would mint
    different DuckDB join tokens without this step. `.normalize()` collapses
    the exponent difference (`Decimal('1.20').normalize() ==
    Decimal('1.2').normalize()`, and both repr identically); zero is also
    canonicalized to a non-negative sign, since `Decimal('-0') ==
    Decimal('0')` but `.normalize()` alone preserves an all-zero value's sign.
    This is a JOIN-KEY-ONLY normalization: `fk_key_value`'s return value (what
    PRESERVE/WARN write back for an orphan) is untouched, so a preserved
    child keeps its own source scale exactly like the oracle does.
    """
    canonical = value.normalize(context=_DECIMAL_JOIN_CONTEXT)
    if canonical.is_zero():
        canonical = abs(canonical)
    return repr(canonical)


def fk_join_key(value: object) -> str:
    """Encode one normalized FK key component for relational joins."""
    normalized = fk_key_value(value)
    if normalized is NULL_FK_KEY:
        return "\x00NULL"
    # `fk_key_value` normalizes bool to int above, so `normalized` is never a
    # bool here; no separate BOOL: tag is needed (or correct -- it would give
    # bool and 0/1 int keys different tokens, undoing the normalization).
    if isinstance(normalized, int):
        return f"\x00INT:{normalized}"
    if isinstance(normalized, float):
        # RI fix (2026-07-25, Codex-confirmed): a FRACTIONAL float and an
        # equal-valued fractional Decimal must mint the SAME join token, because
        # the pandas oracle's plain-dict parent_map already treats them as one
        # key whenever Python's numeric tower says they are equal
        # (`12.5 == Decimal("12.5")` is True). Type-tagging them apart
        # (`\x00FLOAT:` vs `\x00DEC:`) made the out-of-core route disagree with
        # the dict route -- a real referential-integrity divergence (a valid
        # child looked like an orphan on one route only). Encode the float
        # through its EXACT decimal expansion and the same `_decimal_join_token`
        # a Decimal uses, so the token matches IFF the values are truly equal:
        # `Decimal(12.5)` is `Decimal('12.5')` (same token as `Decimal("12.5")`),
        # while `Decimal(0.1)` is the exact 0.1000...0625 expansion (a different
        # token from `Decimal("0.1")`, matching that `0.1 != Decimal("0.1")` in
        # Python). Infinity round-trips through Decimal cleanly; NaN never
        # reaches here (fk_key_value folds it to NULL_FK_KEY).
        return f"\x00DEC:{_decimal_join_token(Decimal(normalized))}"
    if isinstance(normalized, str):
        return f"\x00STR:{len(normalized)}:{normalized}"
    if isinstance(normalized, Decimal):
        # A fractional Decimal (not int-equal, see `fk_key_value` above) is
        # returned unchanged by `fk_key_value` so PRESERVE/WARN keep the
        # child's own scale; the join token still must fold scale-only
        # differences the oracle's dict already folds for free.
        return f"\x00DEC:{_decimal_join_token(normalized)}"
    if isinstance(normalized, dt.datetime):
        # The oracle's dict folds one instant read at two Arrow units (`as_py()` gives
        # `pd.Timestamp` for ns, `datetime` otherwise), so the token is the canonical instant:
        # exact epoch nanoseconds (pandas `Timestamp.value`, Arrow's storage) as an unbounded
        # Python int. NaT (unequal to itself) and zone-aware values keep the OBJ path.
        if normalized.tzinfo is None and normalized == normalized:
            return f"\x00TS:{_epoch_nanoseconds(normalized)}"
    elif isinstance(normalized, dt.date):
        return f"\x00DATE:{normalized.toordinal()}"
    return f"\x00OBJ:{type(normalized).__qualname__}:{normalized!r}"


_EPOCH: Final = dt.datetime(1970, 1, 1)


def _epoch_nanoseconds(value: dt.datetime) -> int:
    nanosecond = 0
    if isinstance(value, pd.Timestamp):
        nanosecond = value.nanosecond
        value = value.to_pydatetime(warn=False)
    delta = value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 10**9 + delta.microseconds * 1_000 + nanosecond


def fk_join_key_tuple(values: tuple[object, ...]) -> str:
    """Encode a full FK key tuple for relational joins.

    Same injective length-prefixed framing idea as `_encode_int`
    (kernel/_canonicalize.py): prefixing each component with its own length keeps
    two differently-shaped key tuples from concatenating into the same joined
    string. (Only the length-PREFIX idea is shared; `_encode_int`'s integer body
    is not DER-minimal, but that is irrelevant to this tuple framing.)
    """
    parts = [fk_join_key(value) for value in values]
    return "".join(f"{len(part)}:{part}" for part in parts)
