"""Invariant-first tests for the pandas-free carrier core (DPS-CODEC phase 1).

These land BEFORE `quality/carriers.py` and drive it to green. The claim
under test is the one the whole redesign exists to protect (guide
2026-07-23-dps-codec-implementation-guide.md, sections 3.1/3.2/3.7/7):

  ADJACENCY. Add or remove one input row changes at most one element of
  the released vector, INCLUDING the row-count projection. This is what
  makes `CarrierTable -> OpenDP vector` stability-1 by construction, so
  the composed `(epsilon, delta)` is honest.

  BOXING INVARIANCE. A codec's verdict is a function of the VALUE modulo
  every reboxing pandas/numpy can apply (bool->complex128 when a `1j` is
  appended, int->float when a null enters, list->length-1 ndarray when a
  column widens to object). A verdict that read the box would drift on an
  ordinary neighbour and break adjacency while OpenDP's `map(1)` stayed
  unchanged.

Both are subject to the section 3.7 residual exclusions (KeyboardInterrupt/
SystemExit from a cell's own hooks propagate; live/executable cells are out
of domain). The crown-jewel properties use Hypothesis (section 7 strategy);
the known defects from the predecessor `dp_normalize.py` matrix are carried
forward as explicit regression SEEDS so this suite is at least as strong
before that module is retired (phase 7).
"""

from __future__ import annotations

import datetime
import decimal
import subprocess
import sys
import warnings
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine.quality.carriers import (
    CarrierError,
    CarrierTable,
    FlagColumn,
    NumberColumn,
    TextColumn,
    decode_flag,
    decode_number,
    decode_text,
    released_values,
    sanitize_carrier_table,
)

# Wide bounds so the numeric codec's clamp never fires in a boxing-invariance
# test; clamping is exercised on its own below.
_WIDE = {"lower": -1e18, "upper": 1e18}


def _multiset_distance(left: Sequence[object], right: Sequence[object]) -> int:
    """Symmetric multiset difference -- the quantity `map(1)` bounds by 1.

    Equal length is NOT this quantity: two neighbours can share a length
    while every element differs, so cardinality would mask a real break."""
    lc, rc = Counter(left), Counter(right)
    return sum(((lc - rc) + (rc - lc)).values())


# ---------------------------------------------------------------------------
# Codec boxing invariance (crown jewel, arm 1) + totality
# ---------------------------------------------------------------------------


def _integral_reboxings(n: int) -> list[Any]:
    """Every box numpy/pandas can hold an integer `n` in that preserves
    its exact value. `n` is bounded to float32's exact-integer range by
    the caller so widening loses nothing."""
    boxes: list[Any] = [
        n,
        float(n),
        np.int64(n),
        np.int32(n),
        np.float64(n),
        np.float32(n),
        complex(n, 0),
        np.complex128(complex(n, 0)),
    ]
    return boxes


def _box_signatures(boxes: Sequence[Any]) -> set:
    """The distinct (python type, numpy dtype) shapes in a reboxing set.

    Mirrors the non-vacuous coverage guard at test_dp.py:688: an invariance
    test asserting a decode set collapses to one element passes VACUOUSLY if
    the inputs share a box, so every invariance test pins that its inputs
    actually span differing boxes -- a future trim of the strategy then fails
    loudly instead of silently proving nothing."""
    return {(type(b).__name__, getattr(b, "dtype", None)) for b in boxes}


class TestCodecBoxingInvariance:
    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    @given(n=st.integers(min_value=-(2**23), max_value=2**23))
    def test_number_codec_is_invariant_across_every_integral_reboxing(self, n: int) -> None:
        boxes = _integral_reboxings(n)
        assert len(_box_signatures(boxes)) >= 6, "reboxing strategy went vacuous"
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # a codec that leaks a warning fails here
            results = {decode_number(box, **_WIDE) for box in boxes}
        assert results == {(float(n), True)}, (n, results)

    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    @given(n=st.integers(min_value=-8, max_value=8))
    def test_flag_codec_collapses_every_reboxing_of_zero_and_one(self, n: int) -> None:
        boxes = _integral_reboxings(n)
        assert len(_box_signatures(boxes)) >= 6, "reboxing strategy went vacuous"
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            results = {decode_flag(box) for box in boxes}
        if n == 0:
            assert results == {(False, True)}, results
        elif n == 1:
            assert results == {(True, True)}, results
        else:
            assert results == {(False, False)}, (n, results)

    @given(f=st.floats(allow_nan=False, allow_infinity=False, min_value=-1e12, max_value=1e12))
    def test_number_codec_is_invariant_across_real_and_zero_imag_complex(self, f: float) -> None:
        boxes = [f, np.float64(f), complex(f, 0), np.complex128(complex(f, 0))]
        assert len(_box_signatures(boxes)) >= 4, "reboxing strategy went vacuous"
        results = {decode_number(box, **_WIDE) for box in boxes}
        # -0.0 is normalized to 0.0, so compare against the codec's own image.
        expected = decode_number(f, **_WIDE)
        assert results == {expected}, (f, results)

    @given(
        s=st.text(
            alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)),
            max_size=40,
        )
    )
    def test_text_codec_is_invariant_across_str_and_numpy_str(self, s: str) -> None:
        # NUL and lone surrogates are excluded here (they have their own seed);
        # this pins that an ordinary str and its numpy box decode identically.
        boxes = [s, np.str_(s)]
        assert len(_box_signatures(boxes)) >= 2, "reboxing strategy went vacuous"
        results = {decode_text(box) for box in boxes}
        assert results == {(s, True)}, (s, results)

    def test_bool_to_complex128_collapse_is_the_named_regression(self) -> None:
        # True in a bool column, then `1j` appended -> whole column complex128,
        # every True reboxed as (1+0j). The flag codec must still read True.
        for box in (
            True,
            np.True_,
            1,
            1.0,
            np.int64(1),
            np.float64(1.0),
            1 + 0j,
            np.complex128(1 + 0j),
        ):
            assert decode_flag(box) == (True, True), box
        for box in (False, np.False_, 0, 0.0, np.int64(0), 0 + 0j, np.complex128(0 + 0j)):
            assert decode_flag(box) == (False, True), box


class TestCodecTotality:
    """Totality (section 3.2): the whole path returns a `(value, valid)`
    pair for ANY data cell and never raises, so fit success cannot become
    a probability-0-vs-1 observable on a one-row neighbour."""

    _CELLS = st.one_of(
        st.integers(min_value=-(2**80), max_value=2**80),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(max_size=30),
        st.booleans(),
        st.none(),
        st.complex_numbers(allow_nan=True, allow_infinity=True),
        st.lists(st.integers(), max_size=4),
        st.binary(max_size=8),
        st.decimals(allow_nan=True, allow_infinity=True),
        st.sampled_from(
            [
                np.int64(3),
                np.float32(1.5),
                np.complex128(1 + 1j),
                np.array([1, 2]),
                np.array(3),
                np.str_("x"),
                np.datetime64("2020-01-01", "ns"),
                np.timedelta64(5, "ns"),
                pd.NA,
                pd.NaT,
                datetime.timedelta(days=1),
                decimal.Decimal("1.5"),
            ]
        ),
    )

    @settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
    @given(cell=_CELLS)
    def test_number_codec_is_total(self, cell: Any) -> None:
        v, ok = decode_number(cell, lower=-100.0, upper=100.0)
        assert isinstance(ok, bool)
        if ok:
            assert isinstance(v, float)
            assert v == v  # not NaN
            assert -100.0 <= v <= 100.0
            assert not (v == 0.0 and str(v) == "-0.0")

    @settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
    @given(cell=_CELLS)
    def test_flag_codec_is_total(self, cell: Any) -> None:
        v, ok = decode_flag(cell)
        assert isinstance(ok, bool)
        if ok:
            assert v is True or v is False

    @settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
    @given(cell=_CELLS)
    def test_text_codec_is_total(self, cell: Any) -> None:
        v, ok = decode_text(cell)
        assert isinstance(ok, bool)
        if ok:
            assert type(v) is str
            assert "\x00" not in v
            v.encode("utf-8")  # never raises for a valid text value


# ---------------------------------------------------------------------------
# Regression SEEDS carried forward from dp_normalize.py's matrix (section 7)
# ---------------------------------------------------------------------------


class TestRegressionSeeds:
    def test_complex_widening(self) -> None:
        # A real re-typed to complex128 by one `1j` neighbour keeps its value
        # in the real part and must still decode; a genuine imaginary drops.
        assert decode_number(2 + 0j, **_WIDE) == (2.0, True)
        assert decode_number(np.complex128(2 + 0j), **_WIDE) == (2.0, True)
        assert decode_number(2 + 1j, **_WIDE) == (0.0, False)
        assert decode_flag(1 + 0j) == (True, True)
        assert decode_flag(1 + 1j) == (False, False)

    @pytest.mark.parametrize(
        "value",
        [np.complex64(1 + 0j), np.complex64(1 + 1j)],
    )
    def test_narrow_complex_width_is_not_silently_realified(self, value: Any) -> None:
        # float(np.complex64(1+1j)) silently returns the real part; the codec
        # must read the imaginary part before converting.
        v, ok = decode_number(value, **_WIDE)
        if value.imag == 0:
            assert (v, ok) == (1.0, True)
        else:
            assert ok is False

    @pytest.mark.parametrize(
        "temporal",
        [
            np.datetime64("2020-01-01", "ns"),
            np.datetime64("2020-01-01", "s"),
            pd.Timestamp("2020-01-01"),
            np.timedelta64(5, "ns"),
            datetime.timedelta(days=1),
            datetime.date(2020, 1, 1),
        ],
    )
    def test_arrow_and_numpy_temporals_are_invalid_for_every_carrier(self, temporal: Any) -> None:
        # `float(np.datetime64(...,'ns'))` SUCCEEDS and returns the epoch
        # integer, so temporal rejection cannot rely on conversion failing.
        assert decode_number(temporal, **_WIDE)[1] is False
        assert decode_flag(temporal)[1] is False
        assert decode_text(temporal)[1] is False

    @pytest.mark.parametrize("container", [[1], np.array([1]), (1,), [1, 2, 3], np.array([1, 2])])
    def test_list_and_ndarray_containers_drop_under_every_boxing(self, container: Any) -> None:
        # `float(np.array([1]))` returns the sole element; the container guard
        # must precede the conversion so a length-1 array drops like a list.
        assert decode_number(container, **_WIDE)[1] is False
        assert decode_flag(container)[1] is False
        assert decode_text(container)[1] is False

    def test_nullable_boolean_cells(self) -> None:
        arr = pd.array([True, False, pd.NA], dtype="boolean")
        fetched = [arr[i] for i in range(len(arr))]
        assert decode_flag(fetched[0]) == (True, True)
        assert decode_flag(fetched[1]) == (False, True)
        assert decode_flag(fetched[2])[1] is False  # pd.NA drops

    def test_nul_and_surrogate_text(self) -> None:
        assert decode_text("a\x00b")[1] is False  # OpenDP truncates at NUL
        assert decode_text("\ud800")[1] is False  # lone surrogate: not UTF-8
        assert decode_text("abc") == ("abc", True)
        assert decode_text("café") == ("café", True)

    def test_text_column_stored_as_int_is_all_invalid_not_stringified(self) -> None:
        # A `text` carrier never calls str(); a numeric cell has no text value.
        for cell in (5, np.int64(5), 5.0, True):
            assert decode_text(cell)[1] is False

    def test_str_subclass_is_rejected_because_invariant_requires_exact_str(self) -> None:
        class Evil(str):
            def encode(self, *a: Any, **k: Any) -> bytes:  # would defeat the FFI check
                return b""

        assert decode_text(Evil("x"))[1] is False

    def test_hostile_dunders_drop_rather_than_abort(self) -> None:
        class Raises:
            def __float__(self) -> float:
                raise RuntimeError("no float")

            def __str__(self) -> str:
                raise RuntimeError("no str")

        cell = Raises()
        assert decode_number(cell, **_WIDE)[1] is False
        assert decode_flag(cell)[1] is False
        assert decode_text(cell)[1] is False

    def test_over_long_int_drops_rather_than_aborting(self) -> None:
        # float(10**10000) overflows; a one-row neighbour carrying it must not
        # take the fit down.
        assert decode_number(10**10000, **_WIDE)[1] is False

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize(
        "decode",
        [
            pytest.param(lambda v: decode_number(v, **_WIDE), id="number"),
            pytest.param(decode_flag, id="flag"),
            pytest.param(decode_text, id="text"),
        ],
    )
    def test_an_interrupt_from_a_cell_hook_propagates(
        self, decode: Any, interrupt: type[BaseException]
    ) -> None:
        # Every codec runs null-detection (`_is_container` -> `np.ndim` -> `__array__`)
        # before its own conversion, so an interrupt raised from a cell's hook must
        # escape all three -- it is never swallowed by the broad drop-to-null guard.
        class Interrupts:
            def __float__(self) -> float:
                raise interrupt()

            def __array__(self, *a: Any, **k: Any) -> Any:
                raise interrupt()

        with pytest.raises(interrupt):
            decode(Interrupts())

    def test_decimal_is_treated_as_a_real_number(self) -> None:
        assert decode_number(decimal.Decimal("1.5"), **_WIDE) == (1.5, True)


# ---------------------------------------------------------------------------
# Numeric codec dispositions (section 3.2)
# ---------------------------------------------------------------------------


class TestNumberCodecDisposition:
    def test_nan_drops(self) -> None:
        assert decode_number(float("nan"), lower=-1.0, upper=1.0)[1] is False

    def test_infinities_clamp_to_the_declared_bound(self) -> None:
        assert decode_number(float("inf"), lower=-1.0, upper=1.0) == (1.0, True)
        assert decode_number(float("-inf"), lower=-1.0, upper=1.0) == (-1.0, True)

    def test_finite_out_of_domain_clamps(self) -> None:
        assert decode_number(5.0, lower=-1.0, upper=1.0) == (1.0, True)
        assert decode_number(-5.0, lower=-1.0, upper=1.0) == (-1.0, True)

    def test_signed_zero_is_normalized(self) -> None:
        v, ok = decode_number(-0.0, lower=-1.0, upper=1.0)
        assert ok is True
        assert v == 0.0 and str(v) == "0.0"


# ---------------------------------------------------------------------------
# Direct CarrierTable adjacency (crown jewel, arm 2) + sanitizer
# ---------------------------------------------------------------------------

_DIRECT_SCHEMA = {
    "number": {"carrier": "number", "bounds": (-1e9, 1e9)},
    "flag": {"carrier": "flag"},
    "text": {"carrier": "text"},
}


@st.composite
def _rows(draw: Any) -> list[tuple[float, bool, str]]:
    n = draw(st.integers(min_value=0, max_value=8))
    number = draw(st.lists(st.floats(allow_nan=True, allow_infinity=True), min_size=n, max_size=n))
    flag = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    text = draw(st.lists(st.text(alphabet=st.characters(), max_size=6), min_size=n, max_size=n))
    return list(zip(number, flag, text, strict=True))


def _table_from_rows(rows: list[tuple[float, bool, str]]) -> CarrierTable:
    n = len(rows)
    valid = np.ones(n, dtype=bool)
    return CarrierTable(
        row_count=n,
        columns={
            "number": NumberColumn(
                values=np.array([r[0] for r in rows], dtype=np.float64), validity=valid.copy()
            ),
            "flag": FlagColumn(
                values=np.array([r[1] for r in rows], dtype=bool), validity=valid.copy()
            ),
            "text": TextColumn(values=tuple(r[2] for r in rows), validity=valid.copy()),
        },
    )


class TestDirectCarrierAdjacency:
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    @given(rows=_rows(), op=st.sampled_from(["add", "remove"]))
    def test_add_or_remove_one_row_changes_each_released_vector_by_at_most_one(
        self, rows: list[tuple[float, bool, str]], op: str
    ) -> None:
        if op == "remove" and not rows:
            op = "add"
        if op == "add":
            neighbour_rows = [*rows, (3.14, True, "z")]
        else:
            k = len(rows) // 2
            neighbour_rows = [r for i, r in enumerate(rows) if i != k]

        with warnings.catch_warnings():
            warnings.simplefilter("error")  # adjacency requires no warning
            base = sanitize_carrier_table(_table_from_rows(rows), _DIRECT_SCHEMA)
            other = sanitize_carrier_table(_table_from_rows(neighbour_rows), _DIRECT_SCHEMA)

        assert abs(base.row_count - other.row_count) == 1
        rel_base = released_values(base)
        rel_other = released_values(other)
        for name in _DIRECT_SCHEMA:
            assert _multiset_distance(rel_base[name], rel_other[name]) <= 1, name

    def test_released_vector_holds_only_valid_cells(self) -> None:
        table = CarrierTable(
            row_count=3,
            columns={
                "number": NumberColumn(
                    values=np.array([1.0, 2.0, 3.0]), validity=np.array([True, False, True])
                )
            },
        )
        out = released_values(
            sanitize_carrier_table(table, {"number": {"carrier": "number", "bounds": (-9.0, 9.0)}})
        )
        assert out["number"] == [1.0, 3.0]


class TestSanitizerFFISafety:
    """A directly-supplied carrier must not smuggle a NaN/NUL/surrogate past
    OpenDP (section 3.1): the sanitizer re-applies the codec FFI-safety
    checks and drops (invalidates) any offending valid cell."""

    def test_direct_nan_marked_valid_is_dropped(self) -> None:
        table = CarrierTable(
            row_count=2,
            columns={
                "number": NumberColumn(
                    values=np.array([np.nan, 5.0]), validity=np.array([True, True])
                )
            },
        )
        out = released_values(
            sanitize_carrier_table(table, {"number": {"carrier": "number", "bounds": (-9.0, 9.0)}})
        )
        assert out["number"] == [5.0]

    @pytest.mark.parametrize("bad", ["a\x00b", "\ud800"])
    def test_direct_nul_or_surrogate_text_marked_valid_is_dropped(self, bad: str) -> None:
        table = CarrierTable(
            row_count=2,
            columns={"t": TextColumn(values=(bad, "ok"), validity=np.array([True, True]))},
        )
        out = released_values(sanitize_carrier_table(table, {"t": {"carrier": "text"}}))
        assert out["t"] == ["ok"]

    def test_row_count_that_is_a_bool_fails_loud(self) -> None:
        # bool is an int subclass; a True row_count must not pass as N=1.
        table = CarrierTable(row_count=True, columns={})
        with pytest.raises(CarrierError):
            sanitize_carrier_table(table, {})

    def test_negative_row_count_fails_loud(self) -> None:
        with pytest.raises(CarrierError):
            sanitize_carrier_table(CarrierTable(row_count=-1, columns={}), {})

    def test_schema_key_mismatch_fails_loud(self) -> None:
        table = CarrierTable(
            row_count=1,
            columns={"a": FlagColumn(values=np.array([True]), validity=np.array([True]))},
        )
        with pytest.raises(CarrierError):
            sanitize_carrier_table(table, {"b": {"carrier": "flag"}})

    def test_length_mismatch_fails_loud(self) -> None:
        table = CarrierTable(
            row_count=3,
            columns={"a": FlagColumn(values=np.array([True]), validity=np.array([True]))},
        )
        with pytest.raises(CarrierError):
            sanitize_carrier_table(table, {"a": {"carrier": "flag"}})

    def test_wrong_column_type_for_declared_carrier_fails_loud(self) -> None:
        table = CarrierTable(
            row_count=1,
            columns={"a": FlagColumn(values=np.array([True]), validity=np.array([True]))},
        )
        with pytest.raises(CarrierError):
            sanitize_carrier_table(table, {"a": {"carrier": "number", "bounds": (0.0, 1.0)}})


class TestMalformedNumberBoundsFailStructurally:
    """Bounds are schema config, not data: a malformed bound is a STRUCTURAL
    error and must fail loud BEFORE any private cell is read, so an empty table
    and its one-row neighbour fail identically. Deferring the check to the
    per-cell clamp made it data-dependent -- an empty table sanitized while its
    neighbour raised (a fit-success adjacency observable), and a NaN bound
    clamped a released value to NaN past OpenDP's nan=False domain (Codex HIGH)."""

    @staticmethod
    def _num_table(rows: list[float]) -> CarrierTable:
        n = len(rows)
        return CarrierTable(
            row_count=n,
            columns={
                "n": NumberColumn(
                    values=np.array(rows, dtype=np.float64),
                    validity=np.ones(n, dtype=np.bool_),
                )
            },
        )

    @pytest.mark.parametrize(
        "bounds",
        [
            ("0", "1"),  # string bounds (the TypeError-on-clamp case)
            (0.0, float("nan")),  # NaN bound (the invalid-NaN-past-FFI case)
            (float("-inf"), 1.0),  # -inf bound
            (0.0, float("inf")),  # +inf bound
            (5.0, 1.0),  # reversed
            (1.0, 1.0),  # zero-width (not lower < upper)
            (None, 1.0),  # non-real
            (0 + 1j, 1.0),  # complex
            (True, 5.0),  # bool bound must not pass as 1.0
            (10**10000, 1.0),  # int too large for float() -> OverflowError, coded
        ],
    )
    def test_malformed_bounds_fail_identically_on_empty_and_one_row(self, bounds: tuple) -> None:
        schema = {"n": {"carrier": "number", "bounds": bounds}}
        codes = []
        for rows in ([], [0.5]):  # empty table and a one-row neighbour
            with pytest.raises(CarrierError) as exc:
                sanitize_carrier_table(self._num_table(rows), schema)
            codes.append(exc.value.code)
        # Same structural failure regardless of row count -> data-independent.
        assert codes[0] == codes[1], codes

    def test_a_bound_whose_float_conversion_raises_becomes_a_coded_error(self) -> None:
        # A numbers.Real whose float() itself raises must normalize to a coded
        # CarrierError, not leak a raw RuntimeError -- and still data-independently.
        class HostileFloat(float):
            def __float__(self) -> float:
                raise RuntimeError("boom")

        schema = {"n": {"carrier": "number", "bounds": (HostileFloat(0.0), 1.0)}}
        codes = []
        for rows in ([], [0.5]):
            with pytest.raises(CarrierError) as exc:
                sanitize_carrier_table(self._num_table(rows), schema)
            codes.append(exc.value.code)
        assert codes == ["dp_carrier_bounds_type", "dp_carrier_bounds_type"]

    def test_well_formed_bounds_still_sanitize(self) -> None:
        schema = {"n": {"carrier": "number", "bounds": (-9.0, 9.0)}}
        out = released_values(sanitize_carrier_table(self._num_table([0.5, -3.0]), schema))
        assert out["n"] == [0.5, -3.0]


# ---------------------------------------------------------------------------
# End-to-end DataFrame arm (phase 2 adapter): the crown jewel over the pandas
# boundary -- THE place the original bug lived (guide sections 3.6/7).
# ---------------------------------------------------------------------------

from decoy_engine.quality.carrier_adapter import (  # noqa: E402  # after the fixtures above
    dataframe_to_carrier_table,
)

_DF_SCHEMA = {
    "num": {"carrier": "number", "bounds": (-1e9, 1e9)},
    "flag": {"carrier": "flag"},
    "text": {"carrier": "text"},
}


def _df(rows: list[tuple[Any, Any, Any]]) -> pd.DataFrame:
    """Build the three-carrier frame from Python cells, letting pandas infer
    each column's storage dtype -- so a neighbour naturally reboxes (int+None
    -> float64, bool+1j -> object, etc.), which is the whole point."""
    return pd.DataFrame(
        {
            "num": [r[0] for r in rows],
            "flag": [r[1] for r in rows],
            "text": [r[2] for r in rows],
        }
    )


def _through_adapter(df: pd.DataFrame) -> CarrierTable:
    # The documented pipeline (task): DataFrame -> adapter -> sanitize. The
    # adapter already certifies its own output, so the outer sanitize is an
    # idempotent no-op; running it anyway pins that the pipeline as specified
    # holds and stays boxing-invariant end to end.
    return sanitize_carrier_table(dataframe_to_carrier_table(df, _DF_SCHEMA), _DF_SCHEMA)


# Cell strategies that span the boxings pandas derives from a whole column, so
# a one-row neighbour reboxes existing cells while OpenDP's map(1) does not.
_NUM_CELL = st.one_of(
    st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.sampled_from([None, float("nan"), float("inf"), float("-inf"), 2 + 0j, 2 + 3j]),
)
_FLAG_CELL = st.one_of(
    st.booleans(),
    st.sampled_from([0, 1, 0.0, 1.0, None, 1j, 2, np.True_, np.False_]),
)
_TEXT_CELL = st.one_of(
    st.text(alphabet=st.characters(), max_size=6),
    st.sampled_from([None, "a\x00b", "\ud800", 5, 1.5, True]),
)


@st.composite
def _df_rows(draw: Any) -> tuple[list[tuple[Any, Any, Any]], tuple[Any, Any, Any]]:
    n = draw(st.integers(min_value=0, max_value=6))
    num = draw(st.lists(_NUM_CELL, min_size=n, max_size=n))
    flag = draw(st.lists(_FLAG_CELL, min_size=n, max_size=n))
    text = draw(st.lists(_TEXT_CELL, min_size=n, max_size=n))
    extra = (draw(_NUM_CELL), draw(_FLAG_CELL), draw(_TEXT_CELL))
    return list(zip(num, flag, text, strict=True)), extra


class TestDataFrameCrownJewel:
    """The end-to-end adjacency invariant over the pandas boundary: for a
    DataFrame and its add/remove-one-row neighbour, `dataframe_to_carrier_table
    -> sanitize_carrier_table -> released_values` changes each column's released
    multiset by at most one INCLUDING the row-count projection, with no raise or
    warning (subject to the section 3.7 exclusions). This is the property the
    original value-level pandas inference violated -- one added row reboxed a
    whole column and moved distance-N labels against a stability-1 certificate."""

    @settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
    @given(data=_df_rows(), op=st.sampled_from(["add", "remove"]))
    def test_add_or_remove_one_row_changes_each_released_vector_by_at_most_one(
        self, data: tuple[list[tuple[Any, Any, Any]], tuple[Any, Any, Any]], op: str
    ) -> None:
        rows, extra = data
        if op == "remove" and not rows:
            op = "add"
        if op == "add":
            base_rows, neighbour_rows = rows, [*rows, extra]
        else:
            k = len(rows) // 2
            base_rows, neighbour_rows = rows, [r for i, r in enumerate(rows) if i != k]

        # Frame construction is caller-side setup (like reading a file); the
        # adapter is the component the DP claim requires not to warn, so the
        # error filter wraps only the adapter+sanitize pipeline.
        base_df, neighbour_df = _df(base_rows), _df(neighbour_rows)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # adapter path must not warn
            base = _through_adapter(base_df)
            other = _through_adapter(neighbour_df)

        assert abs(base.row_count - other.row_count) == 1
        rel_base, rel_other = released_values(base), released_values(other)
        for name in _DF_SCHEMA:
            assert _multiset_distance(rel_base[name], rel_other[name]) <= 1, name

    def test_dataframe_to_carrier_table_end_to_end_adjacency(self) -> None:
        # The original phase-1 xfail seed, now passing: an int-column frame and
        # its one-row-shorter neighbour differ by exactly one released element.
        schema = {"n": {"carrier": "number", "bounds": (-1e9, 1e9)}}
        base = pd.DataFrame({"n": [1.0, 2.0, 3.0]})
        neighbour = pd.DataFrame({"n": [1.0, 2.0]})
        t_base = sanitize_carrier_table(dataframe_to_carrier_table(base, schema), schema)
        t_other = sanitize_carrier_table(dataframe_to_carrier_table(neighbour, schema), schema)
        assert abs(t_base.row_count - t_other.row_count) == 1
        assert _multiset_distance(released_values(t_base)["n"], released_values(t_other)["n"]) <= 1


class TestDataFrameBoxingRegressionSeeds:
    """The specific pandas reboxings the crown jewel exists to survive, pinned
    as explicit seeds so a strategy trim can never silently stop covering them
    (guide section 7). Each asserts the released vector is boxing-invariant and
    the neighbour is distance <= 1."""

    def test_int_plus_none_reboxes_column_to_float_but_values_hold(self) -> None:
        # The canonical case: an int64 column becomes float64 the moment a null
        # enters it, reboxing every existing cell. The released values must not
        # move (1 == 1.0 under OpenDP atomic f64 equality); the null drops.
        schema = {"n": {"carrier": "number", "bounds": (-1e9, 1e9)}}
        base = pd.DataFrame({"n": [1, 2, 3]})
        neighbour = pd.DataFrame({"n": [1, 2, 3, None]})
        assert base["n"].dtype == np.dtype("int64")
        assert neighbour["n"].dtype == np.dtype("float64")
        rel_base = released_values(dataframe_to_carrier_table(base, schema))["n"]
        rel_other = released_values(dataframe_to_carrier_table(neighbour, schema))["n"]
        assert Counter(rel_base) == Counter([1.0, 2.0, 3.0])
        assert Counter(rel_other) == Counter([1.0, 2.0, 3.0])  # None dropped
        assert _multiset_distance(rel_base, rel_other) == 0

    def test_bool_column_with_one_1j_appended_reboxes_but_flags_hold(self) -> None:
        # Appending `1j` widens a bool column to object/complex; every True is
        # reboxed to (1+0j) and must still read True, while the 1j cell drops.
        schema = {"f": {"carrier": "flag"}}
        base = pd.DataFrame({"f": [True, False, True]})
        neighbour = pd.DataFrame({"f": [True, False, True, 1j]})
        assert base["f"].dtype == np.dtype("bool")
        assert neighbour["f"].dtype == np.dtype("object")
        rel_base = released_values(dataframe_to_carrier_table(base, schema))["f"]
        rel_other = released_values(dataframe_to_carrier_table(neighbour, schema))["f"]
        assert [bool(x) for x in rel_base] == [True, False, True]
        assert [bool(x) for x in rel_other] == [True, False, True]  # 1j dropped
        assert _multiset_distance(rel_base, rel_other) == 0

    def test_nullable_boolean_extension_column(self) -> None:
        schema = {"f": {"carrier": "flag"}}
        base = pd.DataFrame({"f": pd.array([True, False, pd.NA], dtype="boolean")})
        neighbour = pd.DataFrame({"f": pd.array([True, False, pd.NA, True], dtype="boolean")})
        rel_base = released_values(dataframe_to_carrier_table(base, schema))["f"]
        rel_other = released_values(dataframe_to_carrier_table(neighbour, schema))["f"]
        assert [bool(x) for x in rel_base] == [True, False]  # pd.NA drops
        assert [bool(x) for x in rel_other] == [True, False, True]
        assert _multiset_distance(rel_base, rel_other) == 1

    def test_nul_and_surrogate_text_cells_drop(self) -> None:
        schema = {"t": {"carrier": "text"}}
        base = pd.DataFrame({"t": ["a", "a\x00b", "\ud800", "c"]})
        released = released_values(dataframe_to_carrier_table(base, schema))["t"]
        assert released == ["a", "c"]  # NUL truncates, surrogate is not UTF-8

    def test_temporal_column_is_all_invalid_for_number_and_text(self) -> None:
        # float(np.datetime64(...,'ns')) SUCCEEDS and returns the epoch integer,
        # so a temporal column must be rejected, never released as its raw ints.
        stamps = [pd.Timestamp("2020-01-01"), pd.Timestamp("2020-01-02")]
        num = dataframe_to_carrier_table(
            pd.DataFrame({"n": stamps}), {"n": {"carrier": "number", "bounds": (-1e18, 1e18)}}
        )
        txt = dataframe_to_carrier_table(pd.DataFrame({"t": stamps}), {"t": {"carrier": "text"}})
        assert released_values(num)["n"] == []
        assert released_values(txt)["t"] == []

    def test_arrow_list_cell_drops_under_both_boxings(self) -> None:
        # An Arrow list<int64> column arrives cell-by-cell as a list; one null
        # widens it to object, reboxing each cell as a length-1 ndarray. Both
        # are containers, not scalars, and must drop identically (float() on a
        # length-1 ndarray would otherwise return its sole element).
        schema = {"n": {"carrier": "number", "bounds": (-1e9, 1e9)}}
        arrow_list = pd.DataFrame(
            {"n": pd.Series(pd.arrays.ArrowExtensionArray(pa.array([[1], [2], [3]])))}
        )
        object_ndarrays = pd.DataFrame(
            {"n": pd.Series([np.array([1]), np.array([2]), np.array([3])])}
        )
        assert released_values(dataframe_to_carrier_table(arrow_list, schema))["n"] == []
        assert released_values(dataframe_to_carrier_table(object_ndarrays, schema))["n"] == []


class TestAdapterTotality:
    """The adapter is total over data values (section 3.7): a cell whose FETCH
    raises is dropped, never allowed to abort the fit -- fit success is itself
    an observable, so a value that took one frame down where its one-row
    neighbour succeeded would break (epsilon, delta)."""

    def test_a_pyarrow_backed_cell_that_cannot_be_fetched_is_dropped_not_crashed(self) -> None:
        # pd.ArrowDtype's positional read calls as_py(), which raises
        # OverflowError when the stored integer leaves the Python datetime
        # range. The unfetchable "ts" cell must drop while the valid "n" column
        # still releases -- no OverflowError escapes the fit.
        unfetchable = pd.Series(
            pd.arrays.ArrowExtensionArray(pa.array([0, 1, 2**60], type=pa.timestamp("s")))
        )
        df = pd.DataFrame({"ts": unfetchable, "n": [1.0, 2.0, 3.0]})
        schema = {
            "ts": {"carrier": "number", "bounds": (-1e18, 1e18)},
            "n": {"carrier": "number", "bounds": (-1e9, 1e9)},
        }
        table = dataframe_to_carrier_table(df, schema)
        assert table.row_count == 3
        rel = released_values(table)
        assert rel["ts"] == []  # temporal + one unfetchable cell, all dropped
        assert rel["n"] == [1.0, 2.0, 3.0]

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
    def test_an_interrupt_from_a_cell_hook_propagates_through_the_adapter(
        self, interrupt: type[BaseException]
    ) -> None:
        # A cell whose __array__ raises an interrupt during null detection must
        # let the interrupt escape (section 3.7), not swallow it into a dropped
        # cell -- an operator's Ctrl-C during a hostile cell must still work.
        class Interrupts:
            def __array__(self, *a: Any, **k: Any) -> Any:
                raise interrupt()

            def __float__(self) -> float:
                raise interrupt()

        df = pd.DataFrame({"n": pd.Series([Interrupts()], dtype=object)})
        schema = {"n": {"carrier": "number", "bounds": (-1e9, 1e9)}}
        with pytest.raises(interrupt):
            dataframe_to_carrier_table(df, schema)


class TestColumnSchemaValidation:
    """`column_schema` and the closed release-kind x carrier table (section 3.3)
    are validated BEFORE any private cell is read, so a malformed schema fails
    loud and data-independently with a coded CarrierError."""

    def test_unknown_carrier_fails_loud(self) -> None:
        with pytest.raises(CarrierError) as exc:
            dataframe_to_carrier_table(pd.DataFrame({"c": [1]}), {"c": {"carrier": "bogus"}})
        assert exc.value.code == "dp_carrier_unknown"

    def test_missing_carrier_fails_loud(self) -> None:
        with pytest.raises(CarrierError) as exc:
            dataframe_to_carrier_table(pd.DataFrame({"c": [1]}), {"c": {"bounds": (0.0, 1.0)}})
        assert exc.value.code == "dp_carrier_unknown"

    def test_number_carrier_without_bounds_fails_loud(self) -> None:
        with pytest.raises(CarrierError) as exc:
            dataframe_to_carrier_table(pd.DataFrame({"n": [1.0]}), {"n": {"carrier": "number"}})
        assert exc.value.code == "dp_carrier_bounds_missing"

    @pytest.mark.parametrize(
        ("kind", "carrier", "ok"),
        [
            ("numeric", "number", True),
            ("categorical", "text", True),
            ("categorical", "flag", True),
            ("categorical", "number", False),  # OpenDP has no float make_count_by
            ("numeric", "text", False),
            ("numeric", "flag", False),
        ],
    )
    def test_closed_kind_carrier_table(self, kind: str, carrier: str, ok: bool) -> None:
        spec: dict[str, Any] = {"kind": kind, "carrier": carrier}
        if carrier == "number":
            spec["bounds"] = (-1.0, 1.0)
        df = pd.DataFrame({"c": [0]})
        if ok:
            dataframe_to_carrier_table(df, {"c": spec})  # no raise
        else:
            with pytest.raises(CarrierError) as exc:
                dataframe_to_carrier_table(df, {"c": spec})
            assert exc.value.code == "dp_kind_carrier_mismatch"

    def test_unknown_kind_fails_loud(self) -> None:
        with pytest.raises(CarrierError) as exc:
            dataframe_to_carrier_table(
                pd.DataFrame({"c": ["x"]}), {"c": {"kind": "ordinal", "carrier": "text"}}
            )
        assert exc.value.code == "dp_kind_unknown"

    def test_kind_is_optional(self) -> None:
        # The phase-1 schemas carry no `kind`; the carrier alone must suffice.
        released = released_values(
            dataframe_to_carrier_table(pd.DataFrame({"t": ["a", "b"]}), {"t": {"carrier": "text"}})
        )
        assert released["t"] == ["a", "b"]

    def test_non_dataframe_source_fails_loud(self) -> None:
        not_a_frame: Any = {"n": [1, 2]}
        with pytest.raises(CarrierError) as exc:
            dataframe_to_carrier_table(
                not_a_frame, {"n": {"carrier": "number", "bounds": (0.0, 9.0)}}
            )
        assert exc.value.code == "dp_adapter_source_type"

    def test_schema_column_absent_from_frame_fails_loud(self) -> None:
        with pytest.raises(CarrierError) as exc:
            dataframe_to_carrier_table(pd.DataFrame({"a": [1]}), {"b": {"carrier": "text"}})
        assert exc.value.code == "dp_adapter_missing_column"

    def test_duplicate_column_label_fails_identically_on_empty_and_one_row(self) -> None:
        # A schema key mapping to two frame columns makes df[name] a DataFrame,
        # not a Series -- a columns-level structural problem. It must fail with a
        # coded error before any cell read, the same way on an empty frame and
        # its one-row neighbour (data-independent), not leak a raw AttributeError.
        schema = {"n": {"carrier": "number", "bounds": (0.0, 9.0)}}
        codes = []
        for rows in ([[], []], [[1.0], [2.0]]):  # empty then one-row neighbour
            frame = pd.DataFrame(dict(enumerate(rows)))
            frame.columns = ["n", "n"]  # duplicate label
            with pytest.raises(CarrierError) as exc:
                dataframe_to_carrier_table(frame, schema)
            codes.append(exc.value.code)
        assert codes == ["dp_adapter_duplicate_column", "dp_adapter_duplicate_column"]

    @pytest.mark.parametrize(
        "bounds",
        [("0", "1"), (0.0, float("nan")), (float("-inf"), 1.0), (5.0, 1.0), (None, 1.0)],
    )
    def test_malformed_bounds_fail_identically_on_empty_and_one_row(self, bounds: tuple) -> None:
        # Bounds are schema config, not data: a malformed bound must fail the
        # same coded way whether the frame is empty or holds a row, so it never
        # becomes a fit-success observable (the phase-1 Codex HIGH, at the
        # adapter boundary).
        schema = {"n": {"carrier": "number", "bounds": bounds}}
        codes = []
        for frame in (pd.DataFrame({"n": pd.Series([], dtype=float)}), pd.DataFrame({"n": [0.5]})):
            with pytest.raises(CarrierError) as exc:
                dataframe_to_carrier_table(frame, schema)
            codes.append(exc.value.code)
        assert codes[0] == codes[1], codes


# ---------------------------------------------------------------------------
# Import isolation (section 3.8 MEDIUM / section 7): the direct-carrier path
# must actually BE pandas/pyarrow-free.
# ---------------------------------------------------------------------------

_ISOLATION_SCRIPT = r"""
import sys, types, os
src = os.environ["DECOY_SRC"]
sys.path.insert(0, src)

# The top-level `decoy_engine` package eagerly imports the mask/generate
# engine (execution._adapter -> pandas), which is out of DPS-CODEC phase 1
# scope. Stub the parent package so only the REAL submodule files load; this
# isolates the carrier subtree's own dependency closure, which is the thing
# the DP guarantee actually rests on.
stub = types.ModuleType("decoy_engine")
stub.__path__ = [os.path.join(src, "decoy_engine")]
sys.modules["decoy_engine"] = stub

# The now-lazy quality package must not pull pandas at import.
import decoy_engine.quality  # noqa: F401
assert "pandas" not in sys.modules, "quality/__init__ pulled pandas"
assert "pyarrow" not in sys.modules, "quality/__init__ pulled pyarrow"

import numpy as np
from decoy_engine.quality import carriers

# Exercise the direct-carrier path end to end.
table = carriers.CarrierTable(
    row_count=2,
    columns={
        "n": carriers.NumberColumn(np.array([1.0, 2.0]), np.array([True, True])),
        "f": carriers.FlagColumn(np.array([True, False]), np.array([True, True])),
        "t": carriers.TextColumn(("a", "b"), np.array([True, True])),
    },
)
schema = {
    "n": {"carrier": "number", "bounds": (-9.0, 9.0)},
    "f": {"carrier": "flag"},
    "t": {"carrier": "text"},
}
released = carriers.released_values(carriers.sanitize_carrier_table(table, schema))
assert released["n"] == [1.0, 2.0]
carriers.decode_number(np.float64(1.0), lower=0.0, upper=10.0)
carriers.decode_flag(np.True_)
carriers.decode_text(np.str_("x"))

leaked = sorted(m for m in sys.modules if m == "pandas" or m.startswith("pandas."))
leaked += sorted(m for m in sys.modules if m == "pyarrow" or m.startswith("pyarrow."))
assert not leaked, "carrier core pulled: " + ", ".join(leaked)
print("ISOLATION_OK")
"""


def test_direct_carrier_path_imports_neither_pandas_nor_pyarrow() -> None:
    import os

    repo_root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "DECOY_SRC": str(repo_root / "src")}
    proc = subprocess.run(  # noqa: S603  # fixed argv, our own interpreter + script constant
        [sys.executable, "-c", _ISOLATION_SCRIPT],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    assert "ISOLATION_OK" in proc.stdout, proc.stdout
