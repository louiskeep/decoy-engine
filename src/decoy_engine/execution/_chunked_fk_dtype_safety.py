"""Predicate 12's EXACT cross-adapter-safe FK-key dtype set (chunked-FK
cascade-safety fix, 2026-09-02 plan).

`hash` is the only FK self-mask strategy admitted onto the chunked route
(`_chunked_fk.gate_fk_child_edges` condition (a), narrowed to hash-only by
this fix); the companion Polars-hash fix routes both adapters through the
same kernel, so hash is cross-adapter byte-identical ONLY for a specific
dtype set. The EXISTING coarse `_dtype_family` comparison (`_chunked_fk.py`)
is too permissive for that claim: it collapses `date32`/`date64`, every
timestamp unit/tz, and every decimal width/scale into one family string, so
it would admit `date64`-as-`date32`, `decimal256`-as-`decimal128`, and a
fixed-offset tz as IANA -- each a real cross-adapter divergence. This module
is the single EXACT predicate both stages of the two-stage check share:

  - `gate_fk_child_edges` (`_chunked_fk.py`) calls `declared_dtype_is_fk_hash_
    safe` at COMPILE time on the operator-DECLARED dtype string (a trusted
    assertion, like the existing family check).
  - The per-chunk runtime guard (`_chunked_fk_dtype.py`) calls
    `arrow_type_is_fk_hash_safe` on the chunk's REAL Arrow type, closing the
    gap a misdeclaration would otherwise leave open.

Declared-string parsing does NOT reuse `pa.type_for_alias`: it does not parse
parameterized timestamp/decimal strings on the pinned PyArrow 24 (confirmed
empirically -- `pa.type_for_alias("timestamp[us, tz=UTC]")` and
`pa.type_for_alias("decimal128(4, -1)")` both raise). Instead each
parameterized alias is parsed with an anchored regex and CONSTRUCTED via the
real PyArrow type constructor (`pa.timestamp(...)` / `pa.decimalNN(...)`),
which validates precision/scale/unit bounds for free; the constructed type is
then judged by the exact same `arrow_type_is_fk_hash_safe` predicate the real
per-chunk data is judged by, so "declared" and "real" can never silently
diverge on what counts as safe.

Extracted as a standalone sibling (rather than folded into either consumer)
specifically to avoid a circular import: `_chunked_fk_dtype.py` already
imports FROM `_chunked_fk.py`, so the reverse direction is unavailable.
"""

from __future__ import annotations

import re
import zoneinfo

import pyarrow as pa

# Non-parameterized declared aliases that map straight to one concrete Arrow
# type. Deliberately includes the loose "string" aliases the coarse family
# helper (`_chunked_fk._dtype_family`) also accepts (str/object/utf8): every
# one of them resolves, at runtime, to either `pa.string()` or
# `pa.large_string()`, and BOTH are individually in the safe set below, so
# accepting any of them here cannot admit an unsafe type. `date64` is
# deliberately ABSENT (only `date32` is safe); a bare "date"/"datetime" (no
# explicit width) is UNPROVABLE by design, mirroring the bare-decimal
# sentinel's fail-closed precedent (`_chunked_fk._DECIMAL_UNPROVABLE_FAMILY`).
_DECLARED_SIMPLE_SAFE_ALIASES: dict[str, pa.DataType] = {
    "bool": pa.bool_(),
    "boolean": pa.bool_(),
    "string": pa.string(),
    "large_string": pa.large_string(),
    "utf8": pa.string(),
    "str": pa.string(),
    "object": pa.string(),
    "date32": pa.date32(),
    "int8": pa.int8(),
    "int16": pa.int16(),
    "int32": pa.int32(),
    "int64": pa.int64(),
    "uint8": pa.uint8(),
    "uint16": pa.uint16(),
    "uint32": pa.uint32(),
    "uint64": pa.uint64(),
}

# PyArrow's own `str()` form: "timestamp[us]" or "timestamp[us, tz=UTC]". The
# tz capture is intentionally greedy-to-close-bracket (`.+?` would also work,
# but `.+` anchored by the trailing `\]$` is unambiguous here) so a tz string
# containing no `]` -- every legal IANA key and fixed-offset string -- passes
# through verbatim, case preserved (`re.IGNORECASE` only affects the literal
# keywords "timestamp"/"tz", not group contents).
_TIMESTAMP_DECLARED_RE = re.compile(
    r"^timestamp\[(?P<unit>s|ms|us|ns)(?:,\s*tz=(?P<tz>.+))?\]$", re.IGNORECASE
)

# PyArrow's own `str()` form with an explicit width: "decimal128(P, S)" (also
# decimal32/64/256). Width is captured so a 256-bit declaration can be
# rejected regardless of scale (via the shared real-type predicate below),
# rather than silently constructing a 128-bit type for a 256-bit declaration.
_DECIMAL_WITH_WIDTH_DECLARED_RE = re.compile(
    r"^decimal(32|64|128|256)\(\s*(\d+)\s*,\s*(-?\d+)\s*\)$", re.IGNORECASE
)

# SQL-style, no width: "decimal(P, S)" / "numeric(P, S)". Ambiguous between a
# 128-bit and 256-bit backing store; constructed as decimal128 (the common
# case) and rejected as unprovable if that construction fails (e.g. precision
# > 38, which would need decimal256 -- itself excluded from the safe set
# regardless of scale, so falling back to it would never help).
_DECIMAL_BARE_WIDTH_DECLARED_RE = re.compile(
    r"^(?:decimal|numeric)\(\s*(\d+)\s*,\s*(-?\d+)\s*\)$", re.IGNORECASE
)


def arrow_type_is_fk_hash_safe(arrow_type: pa.DataType) -> bool:
    """True iff `arrow_type` is in predicate 12's EXACT cross-adapter-safe set
    for hash-only FK self-masking: string, large_string, any signed/unsigned
    integer width, bool, `date32` ONLY, a timestamp (any of s/ms/us/ns) with a
    non-empty `zoneinfo.ZoneInfo`-resolvable tz, or a decimal (32/64/128 -- NOT
    256-bit) with scale >= 0.

    Dictionary-wrapped types are rejected BEFORE unwrapping -- deliberately
    NOT the family helper's dictionary-unwrap-then-compare behavior
    (`_chunked_fk_dtype._arrow_dtype_family`), which is a coarse compatibility
    check, not a cross-adapter byte-parity proof. Hash on a dictionary-encoded
    key is not proven safe here (deferred; see the plan's non-goals).
    """
    if pa.types.is_dictionary(arrow_type):
        return False
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return True
    if pa.types.is_integer(arrow_type):
        return True
    if pa.types.is_boolean(arrow_type):
        return True
    if pa.types.is_date32(arrow_type):
        return True
    if pa.types.is_date64(arrow_type):
        return False
    if pa.types.is_timestamp(arrow_type):
        tz = arrow_type.tz
        if not tz or not tz.strip():
            return False  # tz-naive: no shared instant to canonicalize on
        try:
            zoneinfo.ZoneInfo(tz)
        except Exception:
            return False  # fixed-offset (e.g. "+02:00") or unresolvable
        return True
    if pa.types.is_decimal256(arrow_type):
        return False  # 256-bit excluded regardless of scale
    if pa.types.is_decimal(arrow_type):
        # Covers decimal32/64/128 (decimal256 already excluded above). Scale
        # 0 is hash-identical too -- the bound is >= 0, not > 0.
        return bool(arrow_type.scale >= 0)
    return False


def declared_fk_hash_dtype_is_safe(dtype: str) -> bool:
    """True iff the operator-DECLARED dtype string names a type in predicate
    12's exact cross-adapter-safe set. Returns False for anything unparseable
    (fail closed: an unrecognized or ambiguous declaration is unprovable, same
    posture as the bare-decimal sentinel the coarse family check already
    uses).
    """
    raw = dtype.strip()
    lowered = raw.lower()

    simple = _DECLARED_SIMPLE_SAFE_ALIASES.get(lowered)
    if simple is not None:
        return arrow_type_is_fk_hash_safe(simple)

    ts_match = _TIMESTAMP_DECLARED_RE.match(raw)
    if ts_match is not None:
        unit = ts_match.group("unit").lower()
        tz = ts_match.group("tz")
        try:
            constructed = pa.timestamp(unit, tz=tz) if tz else pa.timestamp(unit)
        except Exception:
            return False
        return arrow_type_is_fk_hash_safe(constructed)

    width_match = _DECIMAL_WITH_WIDTH_DECLARED_RE.match(raw)
    if width_match is not None:
        width, precision, scale = width_match.groups()
        try:
            ctor = getattr(pa, f"decimal{width}")
            constructed = ctor(int(precision), int(scale))
        except Exception:
            return False
        return arrow_type_is_fk_hash_safe(constructed)

    bare_match = _DECIMAL_BARE_WIDTH_DECLARED_RE.match(raw)
    if bare_match is not None:
        precision, scale = bare_match.groups()
        try:
            constructed = pa.decimal128(int(precision), int(scale))
        except Exception:
            return False
        return arrow_type_is_fk_hash_safe(constructed)

    return False  # unrecognized / unprovable (e.g. bare "decimal", "binary")
