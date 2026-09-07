"""Exhaustive branch coverage for predicate 12's exact cross-adapter-safe FK-key
dtype predicate (`_chunked_fk_dtype_safety`). Every admit and every reject branch
of both the real-type predicate and the declared-string parser is pinned here,
because this predicate is the whole cross-adapter byte-parity guard for hash-only
FK self-masking: a reject branch silently flipping to admit would re-open exactly
the divergence the cascade-safety fix closes.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution._chunked_fk_dtype_safety import (
    arrow_type_is_fk_hash_safe,
    declared_fk_hash_dtype_is_safe,
)

# --- arrow_type_is_fk_hash_safe: ADMIT branches ---------------------------------

_SAFE_REAL_TYPES = [
    pa.string(),
    pa.large_string(),
    pa.int8(),
    pa.int16(),
    pa.int32(),
    pa.int64(),
    pa.uint8(),
    pa.uint16(),
    pa.uint32(),
    pa.uint64(),
    pa.bool_(),
    pa.date32(),
    pa.timestamp("s", tz="UTC"),
    pa.timestamp("ms", tz="UTC"),
    pa.timestamp("us", tz="UTC"),
    pa.timestamp("ns", tz="UTC"),
    pa.timestamp("us", tz="America/New_York"),  # IANA non-UTC
    pa.decimal128(10, 0),  # scale 0 is hash-identical -> admitted
    pa.decimal128(10, 2),
    pa.decimal64(10, 2),
    pa.decimal32(5, 2),
]


@pytest.mark.parametrize("t", _SAFE_REAL_TYPES, ids=str)
def test_real_type_safe_admitted(t: pa.DataType) -> None:
    assert arrow_type_is_fk_hash_safe(t) is True


# --- arrow_type_is_fk_hash_safe: REJECT branches (each a distinct return False) --

_UNSAFE_REAL_TYPES = [
    pa.dictionary(pa.int32(), pa.string()),  # dict rejected BEFORE unwrap
    pa.date64(),  # only date32 is safe
    pa.timestamp("us"),  # tz-naive: no shared instant
    pa.timestamp("us", tz="+02:00"),  # fixed-offset, not ZoneInfo-resolvable
    pa.timestamp("us", tz="not/a/zone"),  # unresolvable tz
    pa.decimal256(10, 2),  # 256-bit excluded regardless of scale
    pa.decimal128(10, -1),  # negative scale
    pa.time64("us"),  # unrecognized -> catch-all reject
    pa.time32("s"),  # unrecognized -> catch-all reject
    pa.binary(),  # unrecognized -> catch-all reject
    pa.large_binary(),
    pa.float64(),  # unrecognized -> catch-all reject
    pa.null(),  # not classified safe by this predicate (runtime carveout is separate)
]


@pytest.mark.parametrize("t", _UNSAFE_REAL_TYPES, ids=str)
def test_real_type_unsafe_rejected(t: pa.DataType) -> None:
    assert arrow_type_is_fk_hash_safe(t) is False


def test_decimal256_rejected_even_at_scale_zero() -> None:
    # The decimal256 branch must fire BEFORE the generic is_decimal scale>=0
    # branch, else a scale-0 decimal256 would wrongly admit.
    assert arrow_type_is_fk_hash_safe(pa.decimal256(10, 0)) is False


# --- declared_fk_hash_dtype_is_safe: SAFE declared strings -----------------------

_SAFE_DECLARED = [
    "bool",
    "boolean",
    "string",
    "large_string",
    "utf8",
    "str",
    "object",
    "date32",
    "int8",
    "int32",
    "int64",
    "uint64",
    "timestamp[us, tz=UTC]",
    "timestamp[ns, tz=UTC]",
    "timestamp[s, tz=America/New_York]",
    "TIMESTAMP[US, TZ=UTC]",  # case-insensitive keyword, tz preserved
    "decimal128(10, 2)",
    "decimal128(10, 0)",  # scale 0
    "decimal64(10, 2)",
    "decimal32(5, 2)",
    "decimal(10, 2)",  # bare width -> decimal128
    "numeric(10, 0)",  # bare width, scale 0
]


@pytest.mark.parametrize("s", _SAFE_DECLARED, ids=str)
def test_declared_safe(s: str) -> None:
    assert declared_fk_hash_dtype_is_safe(s) is True


# --- declared_fk_hash_dtype_is_safe: UNSAFE / unparseable declared strings -------

_UNSAFE_DECLARED = [
    "date64",  # deliberately absent from the safe aliases
    "date",  # bare, unprovable width
    "datetime",  # bare, unprovable
    "binary",  # unrecognized -> final reject
    "large_binary",
    "time64[us]",
    "float64",
    "timestamp[us]",  # tz-naive declared
    "timestamp[us, tz=+02:00]",  # fixed-offset declared
    "timestamp[us, tz=UTC\ud800]",  # surrogate in tz: regex matches, pa.timestamp
    # raises UnicodeEncodeError -> the construction except must fail closed (this
    # is the case that makes the timestamp-except reachable; Codex final-gate P2-1)
    "timestamp[xs, tz=UTC]",  # invalid unit -> regex miss -> reject
    "decimal256(10, 2)",  # 256-bit declared
    "decimal128(10, -1)",  # negative scale, explicit-width branch
    "decimal(10, -1)",  # negative scale, bare-width branch (scale must be honored)
    "numeric(8, -2)",  # negative scale, bare-width numeric alias
    "decimal128(50, 2)",  # precision > 38: constructor raises -> reject
    "decimal(50, 2)",  # bare-width construction failure -> reject
    "decimal",  # bare no-width -> reject
    "numeric",  # bare no-width -> reject
    "",  # empty -> reject
    "garbage",
]


@pytest.mark.parametrize("s", _UNSAFE_DECLARED, ids=lambda s: s or "<empty>")
def test_declared_unsafe(s: str) -> None:
    assert declared_fk_hash_dtype_is_safe(s) is False


def test_declared_and_real_agree_on_a_shared_case() -> None:
    # The declared parser judges its constructed type through the SAME predicate
    # the real path uses -- they cannot disagree on what is safe.
    assert declared_fk_hash_dtype_is_safe("timestamp[us, tz=UTC]") is True
    assert arrow_type_is_fk_hash_safe(pa.timestamp("us", tz="UTC")) is True
    assert declared_fk_hash_dtype_is_safe("decimal256(10, 2)") is False
    assert arrow_type_is_fk_hash_safe(pa.decimal256(10, 2)) is False
