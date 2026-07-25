"""Property + metamorphic invariants for the RUNTIME FK key side:
`execution/_fk_keys.py`'s canonical match-key normalization/encoding and its
lossless-integer-typing contract.

TQ (Test-Quality Program) crown-jewels pass. Same family as the pilot
(`tests/property/test_ri_graph_invariants.py`, `relationships/_graph.py`),
but the other half of the RI guarantee: `_graph.py` decides mask ORDER (a
parent must mask before its child); this module decides KEY IDENTITY (a
child FK value must resolve to the SAME masked value its parent got, no
matter which Python type carried the value on either side). A wrong mapping
here silently corrupts referential integrity in the output with no error
raised -- the worst-blast-radius failure mode in the engine.

The invariants below come from `_fk_keys.py`'s own module docstring and each
function's docstring (cited per test):

- RI PRESERVATION (the core): `fk_join_key_tuple` is the token the
  string-keyed routes (out-of-core / DuckDB join) use in place of a plain
  Python dict lookup (which the pandas-backed oracle route gets for free from
  `==`/`hash`). A child FK tuple that matches a parent key must resolve,
  through that token, to the SAME masked value; a genuine orphan must
  resolve to no entry. `test_child_fk_resolves_to_same_parent_masked_key`.
- DETERMINISM: `fk_key_value`, `fk_join_key`, and `fk_join_key_tuple` are
  pure functions -- equal input, equal output, every call.
- CONSISTENCY: `fk_key_value`'s own docstring documents which distinct
  Python types must fold to ONE join token (bool/int/whole-float/whole-
  Decimal all become the same int; None/NaN all become `NULL_FK_KEY`;
  `_decimal_join_token`'s docstring documents that a Decimal's trailing-zero
  SCALE must not affect its token). A fractional float and an equal-valued
  fractional Decimal ALSO fold to one token (RI fix, 2026-07-25): the token
  route encodes a float through its exact decimal expansion, so it matches a
  Decimal IFF Python's numeric tower says they are equal -- see
  `test_fractional_float_and_equal_decimal_share_a_join_token` and
  `test_float_and_decimal_that_are_unequal_keep_distinct_join_tokens`.
- NAMESPACE ISOLATION: this module implements two independent isolation
  mechanisms, both tested directly: `fk_join_key`'s per-type tag prefix
  (`\\x00INT:`/`\\x00STR:`/`\\x00DEC:`/...) keeps different Python types from
  colliding, and `fk_join_key_tuple`'s length-prefixed framing (cited in its
  own docstring as the same idea as the kernel's ASN.1 DER length-prefix
  encoding) keeps differently-shaped key tuples from colliding under naive
  concatenation. `fk_columns_for_table` has its own, simpler table-scoped
  isolation: a table's protected FK columns never include a column that
  belongs only to a DIFFERENT table's edge.
- ORPHAN / FAIL-CLOSED HANDLING: this module does not implement the
  preserve/remap/warn/fail orphan-policy enum itself (that lives in
  `_pandas_adapter.py` / `out_of_core/_join.py`); its own policy-shaped
  behavior is the lossless-dtype fail-closed path
  (`FK_KEY_DTYPE_UNSUPPORTED_CODE`): `lossless_fk_int_values` and
  `fk_nullable_int_array` raise that coded `ExecutionError`, rather than
  silently picking a lossy representation, for every shape the module's own
  docstring says cannot be held exactly.

Run:  pytest tests/property/test_fk_keys_invariants.py -q
"""

from __future__ import annotations

import math
from decimal import Decimal

import pandas as pd
import pyarrow as pa
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import decoy_engine.execution._fk_keys as _fk_keys_module
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import (
    FK_KEY_DTYPE_UNSUPPORTED_CODE,
    NULL_FK_KEY,
    _decimal_join_token,
    fk_all_null_array,
    fk_columns_for_table,
    fk_join_key,
    fk_join_key_tuple,
    fk_key_value,
    fk_nullable_int_array,
    lossless_fk_int_values,
    to_pandas_fk_safe,
)
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

# Match the pilot's audit profile (test_ri_graph_invariants.py): more
# examples than the 100-example default, no deadline (some strategies build
# pyarrow tables / arbitrary-precision Decimals, which can trip the 200ms
# wall on a slow shrink), and print_blob so a counterexample is replayable.
settings.register_profile(
    "audit",
    max_examples=300,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("audit")

# Boundary constants, matching `_fk_keys.py`'s own docstring for
# `fk_nullable_int_array` (kept local rather than importing the module's
# private `_INT64_MIN`/`_INT64_MAX`/`_UINT64_MAX` -- these three numbers ARE
# the documented public contract, so re-deriving them from the docstring is
# itself a small check that the contract hasn't silently drifted).
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_UINT64_MAX = 2**64 - 1

# Smallest odd magnitude beyond which float64's 53-bit mantissa cannot
# represent every integer exactly (module docstring, `_EXACT_FLOAT_INT_BOUND`).
_BIG_INT = 2**53 + 1


def scalar_values() -> st.SearchStrategy[object]:
    """The FK-key scalar domain this module's docstring explicitly claims to
    handle: None, bool, int, float, str, Decimal. Bounded to keep Hypothesis
    shrinking fast and to stay inside `_DECIMAL_JOIN_CONTEXT`'s headroom;
    NaN/infinity are excluded here and covered by dedicated tests instead, so
    a general pairwise property isn't tripped by decimal.py's own NaN
    comparison quirks (unrelated to this module's contract)."""
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(10**9), max_value=10**9),
        st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
        st.text(max_size=6),
        st.decimals(
            min_value=-(10**6), max_value=10**6, allow_nan=False, allow_infinity=False, places=None
        ),
    )


# --------------------------------------------------------------------------
# RI PRESERVATION -- the core invariant, and the reason this module exists.
# --------------------------------------------------------------------------

_RETYPE_KINDS = ("int", "float", "decimal", "bool")


def _retype(x: int, kind: str) -> object:
    if kind == "float":
        return float(x)
    if kind == "decimal":
        return Decimal(x)
    if kind == "bool":
        return bool(x)
    return x


@st.composite
def parent_child_universe(
    draw: st.DrawFn,
) -> tuple[
    tuple[tuple[int, ...], ...], tuple[tuple[tuple[object, ...], tuple[int, ...] | None], ...]
]:
    """A random parent-key set plus a child FK batch, some matching (possibly
    under a DIFFERENT Python numeric type than the parent used) and some
    genuine orphans.

    Parent keys are tuples of small non-negative ints in `[0, 500]`
    (supporting composite keys via `width`). A matching child re-expresses
    the SAME parent tuple under one of `_RETYPE_KINDS` -- `bool` is only
    offered when every element is already 0/1, since `bool(5) == 1 != 5`
    would silently mint a DIFFERENT logical key rather than a retype of the
    same one. An orphan child is the parent's coordinates shifted by
    `+100_000` per element, which is disjoint from the `[0, 500]` parent
    range by construction (no `assume`-based collision filtering needed).
    """
    width = draw(st.integers(min_value=1, max_value=2))
    n_parents = draw(st.integers(min_value=1, max_value=6))
    parents = draw(
        st.lists(
            st.tuples(*(st.integers(min_value=0, max_value=500) for _ in range(width))),
            min_size=n_parents,
            max_size=n_parents,
            unique=True,
        )
    )
    children: list[tuple[tuple[object, ...], tuple[int, ...] | None]] = []
    for parent in parents:
        if draw(st.booleans()):
            allowed_kinds = [
                k for k in _RETYPE_KINDS if k != "bool" or all(v in (0, 1) for v in parent)
            ]
            kind = draw(st.sampled_from(allowed_kinds))
            children.append((tuple(_retype(v, kind) for v in parent), parent))
    n_orphans = draw(st.integers(min_value=0, max_value=4))
    orphan_bases = draw(
        st.lists(
            st.tuples(*(st.integers(min_value=0, max_value=500) for _ in range(width))),
            min_size=n_orphans,
            max_size=n_orphans,
        )
    )
    for base in orphan_bases:
        children.append((tuple(v + 100_000 for v in base), None))
    return tuple(parents), tuple(children)


@given(parent_child_universe())
def test_child_fk_resolves_to_same_parent_masked_key(data) -> None:
    """RI PRESERVATION, the core invariant (module docstring: the reason
    `fk_join_key`/`fk_join_key_tuple` exist at all is that the out-of-core /
    DuckDB join route cannot rely on Python `==`/`hash` the way the
    pandas-backed oracle's plain-dict `parent_map` can, so its string token
    must agree with that dict on every case the dict already folds together).
    Builds a stand-in `parent_map` keyed by `fk_join_key_tuple(parent)` (the
    exact composition the real join uses) and asserts every child FK tuple
    that references an existing parent -- REGARDLESS of which numeric Python
    type carries its value -- resolves to that SAME parent's masked value,
    and every genuine orphan resolves to no entry (no false match, i.e. no
    silent cross-parent corruption)."""
    parents, children = data
    parent_map = {fk_join_key_tuple(p): f"masked_{i}" for i, p in enumerate(parents)}
    assert len(parent_map) == len(parents)  # sanity: parents never collide with each other
    for child_key, origin in children:
        token = fk_join_key_tuple(child_key)
        if origin is not None:
            assert token in parent_map
            assert parent_map[token] == f"masked_{parents.index(origin)}"
        else:
            assert token not in parent_map


# --------------------------------------------------------------------------
# DETERMINISM
# --------------------------------------------------------------------------


@given(scalar_values())
def test_fk_key_value_is_deterministic(value) -> None:
    """`fk_key_value` is a pure function: equal input, equal output."""
    assert fk_key_value(value) == fk_key_value(value)
    assert type(fk_key_value(value)) is type(fk_key_value(value))


@given(scalar_values())
def test_fk_join_key_is_deterministic(value) -> None:
    """`fk_join_key` is a pure function of its input."""
    assert fk_join_key(value) == fk_join_key(value)


@given(st.lists(scalar_values(), min_size=0, max_size=4))
def test_fk_join_key_tuple_is_deterministic(values) -> None:
    """`fk_join_key_tuple` is a pure function of its input."""
    key = tuple(values)
    assert fk_join_key_tuple(key) == fk_join_key_tuple(key)


# --------------------------------------------------------------------------
# CONSISTENCY: types the module's docstring explicitly claims fold together.
# --------------------------------------------------------------------------


@given(st.integers(min_value=-(10**9), max_value=10**9))
def test_integer_equivalent_types_share_one_join_token(n) -> None:
    """CONSISTENCY (`fk_key_value`'s bool branch and `numbers.Number`
    branch): a source key value of `n` arriving as a plain `int`, a
    whole-valued `float`, or a whole-valued `Decimal` -- and, when `n` is 0
    or 1, a `bool` -- must mint the IDENTICAL join token. These are exactly
    the Python types a DB driver may hand back for "the same" integer PK/FK
    value; the pandas oracle's plain-dict `parent_map` already folds all of
    them onto one key via Python's numeric-tower hash/eq contract, and
    `fk_join_key` must agree."""
    variants: list[object] = [n, float(n), Decimal(n)]
    if n in (0, 1):
        variants.append(bool(n))
    tokens = {fk_join_key(v) for v in variants}
    assert len(tokens) == 1
    assert fk_join_key(n) == f"\x00INT:{n}"


@pytest.mark.parametrize("value", [None, math.nan, float("nan"), -math.nan])
def test_null_equivalent_values_share_one_join_token(value) -> None:
    """CONSISTENCY: every null-like source value (a missing PK/FK, or a NaN
    read off a float64 column) collapses to the SAME `NULL_FK_KEY` sentinel
    (`fk_key_value`'s `None`/NaN branches), so a null FK component never
    accidentally matches -- or fails to match -- a null parent component the
    way a real key value would."""
    assert fk_key_value(value) is NULL_FK_KEY
    assert fk_join_key(value) == "\x00NULL"


@given(st.integers(min_value=-(10**6), max_value=10**6))
def test_decimal_scale_only_difference_shares_one_join_token(n) -> None:
    """CONSISTENCY (`_decimal_join_token`'s own docstring): `Decimal('12.50')
    == Decimal('12.5')` and they hash equal (decimal.py's deliberate
    exponent-only equality), so the oracle's dict-keyed `parent_map` already
    treats a parent masked under one scale and a child read under another as
    ONE key. `.normalize()` must fold that trailing-zero scale difference so
    `fk_join_key`'s string token agrees."""
    base_str = f"{n}.5"  # fractional (not int-equal), so this hits the DEC: branch
    base = Decimal(base_str)
    padded = Decimal(base_str + "0")  # same value, different exponent
    assert base == padded
    assert base.as_tuple().exponent != padded.as_tuple().exponent
    assert fk_join_key(base) == fk_join_key(padded)


@given(st.integers(min_value=-(10**6), max_value=10**6))
def test_fractional_float_and_equal_decimal_share_a_join_token(n) -> None:
    """RI fix (2026-07-25, Codex-confirmed): a fractional float and an
    equal-valued fractional Decimal must mint the SAME join token, because the
    pandas full-frame/sequential oracle's plain-dict `parent_map` already
    treats them as ONE key (`N.5 == Decimal('N.5')` under Python's numeric
    tower). The out-of-core string-token route must agree, or a legitimate
    child match becomes a false orphan on that route only. The token route now
    folds a fractional float through its EXACT decimal expansion so it matches
    a Decimal IFF the two values are genuinely equal."""
    value_str = f"{n}.5"
    as_float = float(value_str)
    as_decimal = Decimal(value_str)
    assume(as_float == as_decimal)  # only the genuinely float-exact cases (N.5 always is)
    assert fk_key_value(as_float) == fk_key_value(as_decimal)  # the dict oracle folds these
    assert fk_join_key(as_float) == fk_join_key(as_decimal)  # the token route now folds them too


def test_float_and_decimal_that_are_unequal_keep_distinct_join_tokens() -> None:
    """The flip side of the RI fix: the fold must NOT over-match. A float that
    is not exactly equal to a same-text Decimal (the classic `0.1` case --
    `0.1 != Decimal('0.1')` in Python because the float is not exactly 0.1)
    keeps a DISTINCT token, matching that the dict oracle also keeps them
    apart. Folding these would be a WORSE bug (a spurious FK match)."""
    assert Decimal("0.1") != 0.1  # Python numeric-tower fact this test rests on
    assert fk_join_key(0.1) != fk_join_key(Decimal("0.1"))
    # but the float's own exact expansion round-trips to itself. Decimal(0.1)
    # (the float, not the string) is deliberate here -- it IS the exact value
    # the float carries, which is exactly what fk_join_key encodes a float as.
    assert fk_join_key(0.1) == fk_join_key(Decimal(0.1))  # noqa: RUF032


# --------------------------------------------------------------------------
# CONSISTENCY / INJECTIVITY, general: fk_join_key_tuple's framing.
# --------------------------------------------------------------------------


@given(
    st.lists(scalar_values(), min_size=0, max_size=4),
    st.lists(scalar_values(), min_size=0, max_size=4),
)
def test_join_key_tuple_agrees_with_normalized_equality(t1, t2) -> None:
    """The general two-sided property tying `fk_key_value` normalization to
    `fk_join_key_tuple`'s string encoding: when the two tuples normalize
    (elementwise, via `fk_key_value`) to an EQUAL Python tuple -- same
    length, same values -- their tokens must be equal (CONSISTENCY); when
    the normalized tuples differ, their tokens must differ (INJECTIVITY --
    the property this module's length-prefixed framing exists for: no two
    DIFFERENT key tuples may silently share a token, or the join route would
    attach a child to the wrong parent's masked output). Different-arity
    tuples are included by construction (the two lists are drawn
    independently), directly exercising the framing across tuple SHAPES, not
    just values within one fixed width."""
    key1, key2 = tuple(t1), tuple(t2)
    norm1 = tuple(fk_key_value(v) for v in key1)
    norm2 = tuple(fk_key_value(v) for v in key2)
    token1, token2 = fk_join_key_tuple(key1), fk_join_key_tuple(key2)
    if norm1 == norm2:
        assert token1 == token2
    else:
        assert token1 != token2


def test_join_key_tuple_framing_prevents_concatenation_collision() -> None:
    """Regression pin for the exact adversarial shape `fk_join_key_tuple`'s
    own docstring cites (the ASN.1 DER length-prefix framing idea,
    `kernel/_canonicalize.py::_encode_int`'s lineage): two differently-shaped
    key tuples whose components would concatenate to the same joined text
    under a NAIVE (unframed) join must still encode to different tokens."""
    assert fk_join_key_tuple(("ab", "c")) != fk_join_key_tuple(("a", "bc"))


@given(scalar_values(), scalar_values())
def test_different_python_types_never_share_a_join_token_unless_folded(a, b) -> None:
    """NAMESPACE ISOLATION mechanism #1: `fk_join_key`'s per-type tag prefix
    (`\\x00INT:`/`\\x00STR:`/`\\x00DEC:`/`\\x00OBJ:...`) keeps unrelated types
    from colliding on token text alone. (Floats share the `\\x00DEC:` tag with
    Decimals by design since the RI fix -- an equal-valued float and Decimal
    MUST fold to one token.) Restated as the same two-sided property as the
    tuple version, at the single-value level."""
    if fk_key_value(a) == fk_key_value(b):
        assert fk_join_key(a) == fk_join_key(b)
    else:
        assert fk_join_key(a) != fk_join_key(b)


# --------------------------------------------------------------------------
# ORPHAN / FAIL-CLOSED HANDLING: lossless_fk_int_values, fk_nullable_int_array.
# --------------------------------------------------------------------------


@given(
    st.lists(
        st.one_of(st.none(), st.integers(min_value=-(10**18), max_value=10**18)),
        min_size=0,
        max_size=10,
    )
)
def test_lossless_fk_int_values_pure_int_column_never_raises(values) -> None:
    """A column of ONLY genuine ints (no float, no bool -- see the bool test
    below) and nulls is always classified as pure-int regardless of
    magnitude (module docstring: `if not saw_non_int: return normalized`) --
    there is no float sharing the column to disagree with a large integer
    value, so no dtype choice can corrupt anything and the fail-closed path
    never triggers."""
    result = lossless_fk_int_values(values)
    assert result == values


@given(
    st.integers(min_value=_BIG_INT, max_value=2**60),
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
)
def test_lossless_fk_int_values_raises_when_float_shares_column_with_unrepresentable_int(
    big_int, some_float
) -> None:
    """ORPHAN-shaped fail-closed handling, this module's one real policy
    branch: a resolved FK output column mixing a genuine float with an
    integer beyond exact float64 precision (> 2**53) has no single lossless
    dtype (int64 truncates the fraction; float64 rounds the integer), so the
    module raises the coded `FK_KEY_DTYPE_UNSUPPORTED_CODE` rather than
    silently picking a lossy representation (module docstring, `Raises`
    section)."""
    values = [big_int, some_float, None]
    with pytest.raises(ExecutionError) as ei:
        lossless_fk_int_values(values)
    assert ei.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE


@given(
    st.lists(st.integers(min_value=-(2**52), max_value=2**52), min_size=1, max_size=5),
    st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
)
def test_lossless_fk_int_values_returns_none_when_all_ints_stay_exact_under_float(
    ints, some_float
) -> None:
    """A float sharing a column with only "small" ints (every one within
    +/-2**53) is NOT a precision hazard -- both int64 and float64 could hold
    every value in the column exactly -- so the classifier returns `None`
    (module docstring: 'the caller's existing construction already has no
    precision to lose for this shape') instead of raising."""
    values = [*ints, some_float, None]
    assert lossless_fk_int_values(values) is None


def test_lossless_fk_int_values_excludes_bool_from_the_pure_int_bucket() -> None:
    """`bool` is a `numbers.Integral` subtype but the module deliberately
    keeps it OUT of the pure-int bucket (module docstring: 'matching the
    out-of-core route's own bool/int64 stays in the can't-merge bucket
    rule'), so a column mixing `bool` with a beyond-precision int is treated
    as a non-pure-int (float-shaped) column and raises the SAME fail-closed
    error -- not silently accepted as a pure-int column that happens to
    contain a `bool`."""
    values = [True, False, _BIG_INT]
    with pytest.raises(ExecutionError) as ei:
        lossless_fk_int_values(values)
    assert ei.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE


@given(
    st.lists(
        st.one_of(st.none(), st.integers(min_value=_INT64_MIN, max_value=_INT64_MAX)),
        min_size=0,
        max_size=10,
    )
)
def test_fk_nullable_int_array_round_trips_every_signed_range_value(values) -> None:
    """Every value inside `Int64`'s signed range round-trips through the
    built array EXACTLY, and every `None` stays a null slot (module
    docstring: chooses the narrowest of `Int64`/`UInt64` that holds every
    value exactly)."""
    arr = fk_nullable_int_array(values)
    assert len(arr) == len(values)
    for original, produced in zip(values, list(arr), strict=True):
        if original is None:
            assert produced is pd.NA
        else:
            assert int(produced) == original


@given(
    st.lists(
        st.one_of(st.none(), st.integers(min_value=0, max_value=_UINT64_MAX)),
        min_size=0,
        max_size=10,
    )
)
def test_fk_nullable_int_array_round_trips_every_nonnegative_extended_range_value(values) -> None:
    """A NON-NEGATIVE column spanning the full `UInt64` range (including past
    `Int64.max`, where a genuinely unsigned source key lives) round-trips
    EXACTLY too -- unlike the mixed-sign case below, every value here fits
    the SAME chosen dtype (`UInt64` once any value exceeds `Int64.max`, since
    none are negative)."""
    arr = fk_nullable_int_array(values)
    assert len(arr) == len(values)
    for original, produced in zip(values, list(arr), strict=True):
        if original is None:
            assert produced is pd.NA
        else:
            assert int(produced) == original


@given(
    st.integers(min_value=_INT64_MIN, max_value=-1),
    st.integers(min_value=_INT64_MAX + 1, max_value=_UINT64_MAX),
)
def test_fk_nullable_int_array_fails_closed_when_no_single_dtype_fits_both_signs(
    negative, past_int64_max
) -> None:
    """A THIRD failure shape distinct from the two the module docstring calls
    out by name (a value fitting neither dtype at all): here EACH value
    individually fits `Int64` (the negative one) or `UInt64` (the one past
    `Int64.max`), but no SINGLE dtype can hold both in the same column --
    `UInt64` cannot represent a negative value, `Int64` cannot represent a
    value past its own max. `fk_nullable_int_array` picks `UInt64` (any value
    past `Int64.max` sets `needs_uint64`) and pandas' own construction then
    rejects the negative value, which the function's `except` clause catches
    and re-raises as the SAME coded `FK_KEY_DTYPE_UNSUPPORTED_CODE` rather
    than letting the raw `OverflowError`/`TypeError` escape uncoded. Not
    reachable from a single well-typed Arrow source column in practice (a
    value past `Int64.max` only originates from a genuinely unsigned Arrow
    type, which cannot itself produce a negative Python value), but pinned
    here because the function's own docstring does not separately name this
    shape and a caller depending on `ExecutionError.code` must still see the
    contract hold for it."""
    with pytest.raises(ExecutionError) as ei:
        fk_nullable_int_array([negative, past_int64_max])
    assert ei.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE


@given(st.integers(min_value=2**63, max_value=_UINT64_MAX))
def test_fk_nullable_int_array_selects_uint64_beyond_int64_max(value) -> None:
    """A value beyond signed `Int64`'s range but within `UInt64`'s selects
    `UInt64` (module docstring: 'so a genuine unsigned key in [2**63,
    2**64) ... round-trips instead of raising a raw, uncoded
    OverflowError')."""
    arr = fk_nullable_int_array([value, None])
    assert str(arr.dtype) == "UInt64"
    assert int(arr[0]) == value
    assert arr[1] is pd.NA


@given(st.integers(min_value=_INT64_MIN, max_value=_INT64_MAX))
def test_fk_nullable_int_array_selects_int64_within_signed_range(value) -> None:
    """The common case (module docstring: 'Int64 ... unchanged for the
    overwhelmingly common case') stays on the signed dtype."""
    arr = fk_nullable_int_array([value])
    assert str(arr.dtype) == "Int64"
    assert int(arr[0]) == value


@given(
    st.one_of(
        st.integers(min_value=_INT64_MIN - 10**6, max_value=_INT64_MIN - 1),
        st.integers(min_value=_UINT64_MAX + 1, max_value=_UINT64_MAX + 10**6),
    )
)
def test_fk_nullable_int_array_raises_outside_representable_range(value) -> None:
    """Fail-closed boundary: a resolved key value below `Int64`'s minimum or
    above `UInt64`'s maximum has no lossless dtype to hold it (module
    docstring: 'not reachable from any FK source dtype this engine supports
    today, but checked so a future one fails closed instead of
    corrupting')."""
    with pytest.raises(ExecutionError) as ei:
        fk_nullable_int_array([value])
    assert ei.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE


# --------------------------------------------------------------------------
# fk_all_null_array
# --------------------------------------------------------------------------


@given(
    st.integers(min_value=0, max_value=20),
    st.sampled_from(["Int32", pd.Int64Dtype(), "object", "float64", "int64", "UInt16"]),
)
def test_fk_all_null_array_is_all_null_of_correct_length(length, source_dtype) -> None:
    """An all-null resolved column has no integer value to lose precision on
    (module docstring): every slot is null regardless of `source_dtype`, and
    the array is exactly `length` long, whether `source_dtype` can hold a
    null directly (the common case) or `fk_nullable_int_array`'s fallback had
    to run instead (a plain, non-nullable numpy dtype, e.g. `int64`, with a
    nonzero length)."""
    arr = fk_all_null_array(length, source_dtype)
    assert len(arr) == length
    for value in list(arr):
        assert pd.isna(value)


def test_fk_all_null_array_preserves_a_nullable_source_dtype_exactly() -> None:
    """When `source_dtype` can already hold a null (any pandas nullable
    extension dtype), the output keeps that EXACT dtype rather than falling
    back to `fk_nullable_int_array`'s `Int64` default -- module docstring:
    'that would retype a null-bearing string/uint32/etc. SOURCE column to
    Int64 in the output'."""
    arr = fk_all_null_array(4, pd.UInt32Dtype())
    assert str(arr.dtype) == "UInt32"


def test_fk_all_null_array_falls_back_when_source_dtype_cannot_hold_null() -> None:
    """A plain non-nullable numpy dtype with no null representation (e.g.
    `int64`, once the array is nonempty) cannot hold `None` directly and
    falls back to `fk_nullable_int_array`'s `Int64` default (module
    docstring, `Falls back` sentence)."""
    arr = fk_all_null_array(3, "int64")
    assert str(arr.dtype) == "Int64"
    assert all(pd.isna(v) for v in list(arr))


# --------------------------------------------------------------------------
# fk_columns_for_table -- NAMESPACE ISOLATION mechanism #2 (table scoping).
# --------------------------------------------------------------------------


@st.composite
def edge_universe(draw: st.DrawFn) -> tuple[tuple[str, ...], tuple[RelationshipEdge, ...]]:
    """A random small set of tables and FK edges between them (including
    possible self-referential edges, where a table is its own parent and
    child)."""
    n_tables = draw(st.integers(min_value=2, max_value=5))
    tables = tuple(f"tbl{i}" for i in range(n_tables))
    n_edges = draw(st.integers(min_value=0, max_value=6))
    edges: list[RelationshipEdge] = []
    for i in range(n_edges):
        pt = draw(st.sampled_from(tables))
        ct = draw(st.sampled_from(tables))
        edges.append(
            RelationshipEdge(
                parent_table=pt,
                parent_columns=(f"{pt}_pk_{i}",),
                child_table=ct,
                child_columns=(f"{ct}_fk_{i}",),
                namespace="ns",
                orphan_policy=OrphanPolicy.PRESERVE,
            )
        )
    return tables, tuple(edges)


@given(edge_universe())
def test_fk_columns_for_table_is_scoped_and_isolated(data) -> None:
    """NAMESPACE ISOLATION mechanism #2: `fk_columns_for_table(edges, t)`
    returns exactly the union of columns from edges where `t` is a parent or
    a child -- never a column that belongs ONLY to some other table's edge.
    The equality check below proves both completeness (every column that
    SHOULD be protected is present) and isolation (nothing else leaks in) in
    one assertion, since `expected` is built the same way the function's own
    docstring describes ('Every column on `table` that is a relationship
    edge's parent or child key')."""
    tables, edges = data
    for t in tables:
        cols = fk_columns_for_table(edges, t)
        expected: set[str] = set()
        for e in edges:
            if e.parent_table == t:
                expected.update(e.parent_columns)
            if e.child_table == t:
                expected.update(e.child_columns)
        assert cols == expected


def test_fk_columns_for_table_self_referential_edge_includes_both_roles() -> None:
    """A self-referential FK (a table is its own parent AND child, e.g. an
    `employees.manager_id -> employees.id` hierarchy) contributes BOTH its
    parent and child columns to that one table's protected set."""
    edge = RelationshipEdge(
        parent_table="employees",
        parent_columns=("id",),
        child_table="employees",
        child_columns=("manager_id",),
        namespace="ns",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    assert fk_columns_for_table((edge,), "employees") == {"id", "manager_id"}


# --------------------------------------------------------------------------
# to_pandas_fk_safe -- the ingestion half of the lossless-typing contract.
# --------------------------------------------------------------------------


@given(
    st.lists(
        st.one_of(st.none(), st.integers(min_value=_INT64_MIN, max_value=_INT64_MAX)),
        min_size=0,
        max_size=15,
    )
)
def test_to_pandas_fk_safe_preserves_int64_fk_column_exactly(values) -> None:
    """DE-10 ingestion half of the lossless-typing contract (module
    docstring 'Root cause'): an Arrow int64 column with a null must not
    silently widen to float64 (which rounds any value beyond 2**53) when the
    column is named as FK-relevant, regardless of the actual magnitude drawn
    here (Hypothesis will find the > 2**53 case on its own)."""
    table = pa.table({"k": pa.array(values, type=pa.int64())})
    out = to_pandas_fk_safe(table, {"k"})
    assert str(out["k"].dtype) == "Int64"
    for original, produced in zip(values, out["k"].tolist(), strict=True):
        if original is None:
            assert pd.isna(produced)
        else:
            assert int(produced) == original


def test_to_pandas_fk_safe_leaves_non_fk_columns_unprotected() -> None:
    """`fk_columns` is a targeted allowlist, not a blanket policy (module
    docstring: 'a targeted fix for FK referential-integrity data, not a
    blanket dtype policy change for every integer column the engine
    masks'): a column NOT named in `fk_columns` keeps pyarrow's ordinary
    `to_pandas()` default (a null-bearing int64 column widens to float64),
    even though it sits right next to a protected column that does not."""
    table = pa.table(
        {
            "fk_col": pa.array([1, None, 2**60], type=pa.int64()),
            "plain_col": pa.array([1, None, 2**60], type=pa.int64()),
        }
    )
    out = to_pandas_fk_safe(table, {"fk_col"})
    assert str(out["fk_col"].dtype) == "Int64"
    assert str(out["plain_col"].dtype) == "float64"


# --------------------------------------------------------------------------
# TQ crown-jewels mutation-kill pass (docs/quality/mutation-ledgers/execution_fk_keys.md).
# The properties above already pin every documented invariant; the tests
# below target LOGIC survivors from the mutmut run that no existing test
# happened to exercise: the numbers.Number NaN comparison, the Decimal
# join-token's context/zero-sign/repr steps, the OBJ: type tag, the tuple
# framing's empty join separator, the float-representable boundary, the
# pd.isna null classification (including its except fallback), a
# continue-vs-break loop-control mutation in three different loops, and two
# dtype-boundary comparisons. Equivalent (message-prose-only or true no-op)
# survivors are left alive and ledgered, not tested here.
# --------------------------------------------------------------------------


def test_fk_key_value_number_nan_collapses_to_null_fk_key() -> None:
    """`is_nan = value != value` (the `numbers.Number` branch's only way to
    detect NaN, since not every Number subtype exposes `math.isnan`) must
    stay a real comparison: `Decimal("NaN")` is a `numbers.Number` that is
    not `Integral`/`float`, so it reaches this exact line. A mutated
    `is_nan = None` is falsy just like `False`, so the NaN check never fires
    and the function instead falls into the `int(value)` conversion below,
    which raises `ValueError` on a NaN Decimal and is swallowed by the
    broad `except` -- returning the raw NaN Decimal unchanged instead of the
    `NULL_FK_KEY` sentinel every other null-like value collapses to."""
    assert fk_key_value(Decimal("NaN")) is NULL_FK_KEY


def test_decimal_join_token_preserves_precision_beyond_default_context() -> None:
    """`_decimal_join_token`'s `.normalize()` call must use the module's wide
    `_DECIMAL_JOIN_CONTEXT` (prec=200), not the ambient default context
    (prec=28): a 41-significant-digit fractional Decimal exceeds the
    default's precision, so normalizing under the default context would
    silently ROUND it before the token is minted -- corrupting the join key
    for a legitimate high-precision Arrow decimal128/decimal256 value. Uses
    a fractional value (not int-equal) so `fk_key_value` does not fold it to
    a plain `int` first and this actually reaches the Decimal branch."""
    value = Decimal("1." + "1" * 40)
    assert len(value.as_tuple().digits) > 28  # exceeds decimal's default 28-digit context
    assert fk_join_key(value) == f"\x00DEC:{value!r}"


def test_decimal_join_token_zero_sign_is_canonicalized_to_non_negative() -> None:
    """`_decimal_join_token`'s own docstring: `.normalize()` alone preserves
    an all-zero value's sign (`Decimal('-0')` stays negative-zero), so the
    function explicitly re-canonicalizes a zero result via `abs(canonical)`.
    Tested directly against the private helper (not through `fk_join_key`)
    because `fk_key_value` folds any int-equal Decimal -- including every
    zero, regardless of scale or sign -- to a plain `int` before it would
    ever reach this function through the only production call site; the
    helper's own zero-canonicalization branch is otherwise unreachable via
    the public API (see report: DEAD-BRANCH observation, not a bug)."""
    assert _decimal_join_token(Decimal("0")) == "Decimal('0')"
    assert _decimal_join_token(Decimal("-0")) == "Decimal('0')"


def test_decimal_join_token_reflects_the_actual_value() -> None:
    """`return repr(canonical)` must return the CANONICALIZED VALUE's repr,
    not a constant: a mutated `repr(None)` would mint the identical
    `"\\x00DEC:None"` token for every distinct Decimal, silently merging
    every fractional-Decimal FK key into one join bucket."""
    assert fk_join_key(Decimal("5.5")) == "\x00DEC:Decimal('5.5')"
    assert fk_join_key(Decimal("5.5")) != fk_join_key(Decimal("7.25"))


def test_fk_join_key_obj_tag_uses_the_value_s_actual_type_name() -> None:
    """The catch-all `\\x00OBJ:{type(normalized).__qualname__}:{normalized!r}`
    branch (anything `fk_key_value` returns unchanged: not None/bool/int/
    float/str/Decimal) must tag with the VALUE's real type, not a constant
    `NoneType` -- otherwise two different unsupported types would collide on
    the same OBJ: token whenever their `repr()` also happened to coincide,
    and every OBJ:-tagged key would misreport its own type."""
    assert fk_join_key([1, 2]) == "\x00OBJ:list:[1, 2]"
    assert fk_join_key((1, 2)) == "\x00OBJ:tuple:(1, 2)"


@given(scalar_values(), scalar_values())
def test_fk_join_key_tuple_uses_no_separator_between_components(a, b) -> None:
    """`"".join(...)` -- the empty-string separator -- is why concatenating
    two SINGLE-component tuples' tokens equals the TWO-component tuple's
    token: a `join` call over exactly one part never inserts a separator
    (there is nothing to separate), so `fk_join_key_tuple((a,))` and
    `fk_join_key_tuple((b,))` are each just their one length-prefixed part,
    and the real "".join over both parts is plain concatenation. A mutated
    non-empty separator (e.g. `"XXXX".join(...)`) would insert junk between
    the two parts in the two-component call but not in either one-component
    call, breaking this identity -- while `fk_join_key_tuple`'s own
    injectivity properties above are indifferent to which separator is used
    (they only compare tokens to each other), so they cannot catch this."""
    token_a = fk_join_key_tuple((a,))
    token_b = fk_join_key_tuple((b,))
    assert token_a + token_b == fk_join_key_tuple((a, b))


@given(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False))
def test_lossless_fk_int_values_exact_float_bound_is_inclusive_on_both_sides(some_float) -> None:
    """`_is_exactly_float_representable`'s docstring-cited bound
    (`_EXACT_FLOAT_INT_BOUND = 2**53`) is INCLUSIVE on both ends (module
    docstring: 'the largest magnitude every integer up to AND INCLUDING it
    can round-trip through float64 exactly'). A column pairing a genuine
    float with an int at EXACTLY `+2**53` or `-2**53` must NOT be flagged
    unrepresentable -- a `<`/`<=` boundary flip on either side would
    wrongly fail-closed on the one value each bound is supposed to still
    admit."""
    bound = 2**53
    assert lossless_fk_int_values([bound, some_float, None]) is None
    assert lossless_fk_int_values([-bound, some_float, None]) is None


def test_lossless_fk_int_values_pd_na_is_classified_as_a_null_slot() -> None:
    """`is_null = bool(pd.isna(value))` is how a masked value read back off a
    nullable `Int64` parent column (`pd.NA`, not plain `None`) is recognized
    as a null FK component (module docstring: 'pd.NA ... a masked value read
    back off a nullable Int64 parent column'). A mutated `is_null = None` (or
    `bool(None)`) is unconditionally falsy, so `pd.NA` would instead fall
    into the non-null/non-Integral bucket and flip `saw_non_int` -- silently
    turning a legitimate all-int-plus-nulls column into a 'not pure int'
    classification."""
    assert lossless_fk_int_values([pd.NA, 5, 10]) == [None, 5, 10]


def test_lossless_fk_int_values_array_like_element_falls_to_non_int_bucket() -> None:
    """The `except (TypeError, ValueError): is_null = False` fallback (for a
    `pd.isna` call that returns an array-like result instead of a scalar
    bool, e.g. because the FK value component is itself list-shaped) must
    stay `False` -- letting the value fall through to the ordinary
    Integral/non-Integral classification below -- NOT `True`, which would
    misclassify a genuinely unclassifiable value as a null slot and hide it
    from `saw_non_int`, silently turning a column that should return `None`
    (not a pure-int shape) into one this function reports as pure-int."""
    values = [[1, 2], 5]  # a list-shaped element trips pd.isna's array-like path
    assert lossless_fk_int_values(values) is None


def test_lossless_fk_int_values_processes_every_value_after_a_null() -> None:
    """The null-slot branch's `continue` must move on to the NEXT value, not
    terminate the loop (a mutated `break` would silently truncate the
    classified column at the first null, discarding every later value)."""
    assert lossless_fk_int_values([None, 1, 2, 3]) == [None, 1, 2, 3]


def test_fk_nullable_int_array_uint64_max_is_a_valid_boundary_not_an_overflow() -> None:
    """`if value > _UINT64_MAX:` must be strictly-greater: `_UINT64_MAX`
    itself (`2**64 - 1`) is `UInt64`'s own maximum representable value and
    must round-trip, not raise. A mutated `>=` would wrongly reject the one
    value at the boundary that IS exactly representable."""
    uint64_max = 2**64 - 1
    arr = fk_nullable_int_array([uint64_max])
    assert str(arr.dtype) == "UInt64"
    assert int(arr[0]) == uint64_max


def test_fk_nullable_int_array_int64_min_is_a_valid_boundary_not_an_overflow() -> None:
    """`elif value < _INT64_MIN:` must be strictly-less: `_INT64_MIN`
    (`-(2**63)`) itself is `Int64`'s own minimum representable value and
    must round-trip, not raise. A mutated `<=` would wrongly reject the one
    value at the boundary that IS exactly representable."""
    int64_min = -(2**63)
    arr = fk_nullable_int_array([int64_min])
    assert str(arr.dtype) == "Int64"
    assert int(arr[0]) == int64_min


def test_to_pandas_fk_safe_skips_an_absent_fk_column_and_still_protects_the_rest() -> None:
    """`if col not in table.column_names: continue` must move on to the NEXT
    named FK column, not abandon the loop -- a mutated `break` would silence
    protection for every FK column listed AFTER the first one that happens
    to be absent from this particular table (e.g. a relationship edge whose
    other side lives on a different table)."""
    table = pa.table({"present_int": pa.array([1, None, 2**60], type=pa.int64())})
    out = to_pandas_fk_safe(table, ["missing_col", "present_int"])
    assert str(out["present_int"].dtype) == "Int64"


def test_to_pandas_fk_safe_skips_a_non_integer_fk_column_and_still_protects_the_rest() -> None:
    """`if not pa.types.is_integer(arrow_type): continue` must move on to the
    NEXT named FK column, not abandon the loop -- a mutated `break` would
    silence protection for every FK column listed AFTER the first
    non-integer one (e.g. a string-typed natural key column sharing a table
    with an integer surrogate FK column)."""
    table = pa.table(
        {
            "str_col": pa.array(["a", "b", "c"], type=pa.string()),
            "int_col": pa.array([1, None, 2**60], type=pa.int64()),
        }
    )
    out = to_pandas_fk_safe(table, ["str_col", "int_col"])
    assert str(out["int_col"].dtype) == "Int64"


def test_to_pandas_fk_safe_cast_failure_raises_the_coded_execution_error(monkeypatch) -> None:
    """The `except (pa.lib.ArrowInvalid, OverflowError) as exc:` handler must
    re-raise with `code=FK_KEY_DTYPE_UNSUPPORTED_CODE` -- the SAME coded
    error every other route in this module raises for a genuinely
    unrepresentable FK key (module docstring). A mutated `code=None`, or the
    `code=` kwarg dropped entirely (which raises a bare `TypeError` from
    `ExecutionError.__init__`'s required keyword-only parameter instead of
    a coded `ExecutionError`), both break a caller that branches on
    `ExecutionError.code`. The cast failure itself is not reachable from any
    well-formed Arrow table (module docstring: 'not reachable ... but a
    fail-closed backstop'), so the `types_mapper` hook is monkeypatched to
    force it."""

    def _always_fails(_arrow_type: object) -> None:
        raise pa.lib.ArrowInvalid("forced for test")

    monkeypatch.setattr(_fk_keys_module, "_exact_fk_types_mapper", _always_fails)
    table = pa.table({"k": pa.array([1, 2, 3], type=pa.int64())})
    with pytest.raises(ExecutionError) as ei:
        to_pandas_fk_safe(table, {"k"})
    assert ei.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE
