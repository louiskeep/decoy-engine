"""Unit tests for `decoy_engine.quality.dp.fit_dp_snapshot` (DPS Scope B).

Supersedes the Option A `apply_dp_noise` suite entirely (that mechanism
is deleted). Covers the guide's named assertions for step 5 section 3
(config validation, release ID minting, disclosure-channel regressions,
categorical order/other_count derivation) plus the section 7.2 unseeded
statistical mechanism tests.
"""

from __future__ import annotations

import datetime
import decimal
import gc
import itertools
import json
import logging
import math
import numbers
import warnings
from collections import Counter
from collections.abc import Sequence
from fractions import Fraction
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.quality.carriers import CarrierError
from decoy_engine.quality.dp import (
    DpError,
)
from decoy_engine.quality.dp import (
    _fit_dp_snapshot_with_backend as _real_fit_dp_snapshot_with_backend,
)
from decoy_engine.quality.dp import (
    fit_dp_snapshot as _real_fit_dp_snapshot,
)
from decoy_engine.quality.dp_budget import DpBudgetError, _FakeMeasurement
from decoy_engine.quality.dp_normalize import _cells, _normalize_categorical, _normalize_numeric


def _schema_from(categorical_columns=(), numeric_domains=None):
    """Build a `column_schema` (the DPS-CODEC phase-5 fit API, guide section
    3.5) from the legacy `categorical_columns`/`numeric_domains` kwargs this
    suite was written against: a `text` carrier per categorical column, a
    `number` carrier + `(lower, upper)` bounds per numeric column. This is the
    spec-mandated signature migration (guide sections 3.5/6, pre-GA hard break),
    NOT a behaviour change -- the real new-signature `fit_dp_snapshot` is
    exercised unchanged, and the number/text release stays byte-identical (see
    `TestCarrierPathReproducesNormalize` in test_dp_codec_golden.py)."""
    schema: dict[str, dict] = {}
    for c in categorical_columns:
        schema[c] = {"kind": "categorical", "carrier": "text"}
    for c, bounds in (numeric_domains or {}).items():
        schema[c] = {"kind": "numeric", "carrier": "number", "bounds": bounds}
    return schema


def fit_dp_snapshot(
    frame,
    *,
    categorical_columns=(),
    numeric_domains=None,
    epsilon,
    delta,
    numeric_bins=10,
):
    """Legacy-signature -> `column_schema` translation shim for this suite (see
    `_schema_from`). Calls the real phase-5 `fit_dp_snapshot(source,
    column_schema, ...)`."""
    return _real_fit_dp_snapshot(
        frame,
        _schema_from(categorical_columns, numeric_domains),
        epsilon=epsilon,
        delta=delta,
        numeric_bins=numeric_bins,
    )


def _fit_dp_snapshot_with_backend(
    frame,
    *,
    categorical_columns=(),
    numeric_domains=None,
    epsilon,
    delta,
    numeric_bins=10,
    _session_backend=None,
):
    """Legacy-signature translation shim for the private backend seam."""
    return _real_fit_dp_snapshot_with_backend(
        frame,
        _schema_from(categorical_columns, numeric_domains),
        epsilon=epsilon,
        delta=delta,
        numeric_bins=numeric_bins,
        _session_backend=_session_backend,
    )

# The LITERAL name, not `_policy_logger.name` -- deriving it from the
# module under test compares the value to itself, which is how a
# garbage-policy mutant survived twice earlier in this program.
_POLICY_LOGGER_NAME = "decoy_engine.quality.dp_policy"

# Fixed test-confidence budget (guide section 7.2): 1e-6 false-failure
# probability per statistical test per run, derived once as a module
# constant so a later reader can recheck the arithmetic.
_ALPHA = 1e-6


class _RaisesOnConversion:
    """A scalar whose `float()` and `str()` both raise something no
    handler would think to enumerate. Row content is caller-supplied, so
    normalization cannot assume any conversion is total."""

    def __float__(self) -> float:
        raise RuntimeError("no float for you")

    def __str__(self) -> str:
        raise RuntimeError("no str for you")

    def __repr__(self) -> str:
        return "<_RaisesOnConversion>"


# C-B4: each entry raises a different exception type from `float()` and
# `str()`. `10**10000` overflows the float conversion and trips CPython's
# 4300-digit integer-to-string cap; `_RaisesOnConversion` raises an
# unrelated type from both.
_HOSTILE_SCALARS = (10**10000, _RaisesOnConversion())


class _WarnsOnConversion:
    """A scalar whose conversion emits a warning.

    D-H1 (dennis round 4): the blanket warning suppression is the whole
    remediation for the `ComplexWarning` disclosure channel, and nothing
    could falsify removing it. Every existing test used a builtin
    `complex`, which the complex guard drops before `float()` is ever
    reached, so no warning could be emitted with or without the
    suppression. Complex values can no longer serve as the probe at all
    now that every complex width is dropped by kind. The suppression's
    actual claim is content-independent ("no warning ever, whatever the
    row holds"), so the probe is a value that warns during conversion
    itself, which tests the invariant rather than one historical
    instance of it.
    """

    def __float__(self) -> float:
        warnings.warn("converting to float", UserWarning, stacklevel=1)
        return 1.0

    def __str__(self) -> str:
        warnings.warn("rendering as str", UserWarning, stacklevel=1)
        return "1"


# Codex round 5: the keys in a DP column block that may legitimately
# differ between two frames, because each is (or is derived from) a
# quantity OpenDP released under noise. Every OTHER key must be a public
# constant, identical across any two frames under the same declaration.
# Comparing key SETS alone is not enough: a leak of the shape
# `block["all_null"] = not values` adds the key unconditionally, so both
# neighbours carry it and only the VALUE differs.
_RELEASED_COLUMN_KEYS = frozenset({"null_count", "non_null_count", "distinct_count"})
_RELEASED_STATS_KEYS = frozenset({"bin_counts", "top_values", "other_count"})


def _multiset_distance(left: Sequence[object], right: Sequence[object]) -> int:
    """Symmetric multiset difference -- the quantity `map(1)` bounds by 1.

    Cardinality is NOT this quantity: two neighbours can have equal
    length while every element differs (dennis round 7, HIGH-1)."""
    lc, rc = Counter(left), Counter(right)
    return sum(((lc - rc) + (rc - lc)).values())


# The adjacency matrix for the recordwise property. Pools straddle
# float64's exact integer range in both signs, because that boundary is
# where the round-6 canonicalization silently stopped working; `_obj`
# pools are only constructible as object dtype.
_ADJACENCY_POOLS: dict[str, tuple[list, bool]] = {
    "small_int": ([1, 2, 3], False),
    "boundary_int": ([2**53 + 1, 2**53 + 3], False),
    "big_int": ([2**60 + 1, 2**60 + 3, 2**60 + 5], False),
    "neg_big_int": ([-(2**60 + 1), -(2**60 + 3)], False),
    "uint64": (list(np.array([2**64 - 1, 2**64 - 3], dtype=np.uint64)), False),
    "float64": ([1.5, 2.5], False),
    "integral_float": ([7.0, 8.0], False),
    "float32": (list(np.array([0.1, 0.2], dtype=np.float32)), False),
    "string": (["a", "b"], False),
    "bool": ([True, False], False),
    "timedelta": ([datetime.timedelta(days=1), datetime.timedelta(days=2)], False),
    "datetime": ([np.datetime64("2020-01-01"), np.datetime64("2020-01-02")], False),
    "timestamp": ([pd.Timestamp("2020-01-01"), pd.Timestamp("2020-01-02")], False),
    "mixed_num_str": ([1, "a", 2.5], False),
    "huge_int_obj": ([10**400, 10**401], True),
    "decimal_obj": ([decimal.Decimal("1.5"), decimal.Decimal("2.5")], True),
    # dennis round 8: extension dtypes were absent, and `boolean` is
    # where the whole-column drop lived (numpy.bool_ is neither `bool`
    # nor `numbers.Real`).
    "ext_boolean": ([True, False], False),
    "ext_Int64": ([1, 2**60 + 1], False),
    "ext_Float64": ([1.5, 2.5], False),
    "ext_string": (["a", "b"], False),
    "complex_real": ([1 + 0j, 2 + 0j], False),
    # dennis round 9: the day-resolution `datetime` pool above was the
    # ONE unit that dodges the defect. `.item()` returns a datetime for
    # second and microsecond resolution but an INT for nanosecond, which
    # is pandas' native one, so only the ns pools falsify the gate.
    "datetime_ns": ([np.datetime64("2020-01-01", "ns"), np.datetime64("2020-01-02", "ns")], False),
    "datetime_us": ([np.datetime64("2020-01-01", "us"), np.datetime64("2020-01-02", "us")], False),
    "datetime_s": ([np.datetime64("2020-01-01", "s"), np.datetime64("2020-01-02", "s")], False),
    "timedelta_ns": ([np.timedelta64(5, "ns"), np.timedelta64(7, "ns")], False),
    "timedelta_us": ([np.timedelta64(5, "us"), np.timedelta64(7, "us")], False),
    "decimal_mixed": ([decimal.Decimal("1.5"), decimal.Decimal("2")], True),
    # dennis round 10: pandas ships THREE dtype backends and the matrix
    # covered two. `pd.ArrowDtype` is reachable without any adversary via
    # `read_parquet`/`read_csv(dtype_backend="pyarrow")` or
    # `convert_dtypes(dtype_backend="pyarrow")`, and pyarrow is a hard
    # dependency. Its `__iter__` calls `as_py()` per element, which
    # raised on an out-of-Python-range temporal and took the whole fit
    # down with it (BLOCKER-1). The out-of-range pools below are the
    # falsifying ones: `[ns]` is the only SAFE arrow resolution, the
    # exact inverse of round 9's numpy finding.
    "arrow_ts_s": ([0, 1], False),
    "arrow_ts_s_overflow": ([0, 2**60], False),
    "arrow_ts_us_overflow": ([0, 2**62], False),
    "arrow_ts_ns": ([0, 1], False),
    "arrow_duration_s_overflow": ([0, 2**60], False),
    "arrow_date32_overflow": ([0, 2**30], False),
    "arrow_time64_us_overflow": ([0, 2**60], False),
    "arrow_int64": ([1, 2**60 + 1], False),
    "arrow_string": (["a", "b"], False),
    "arrow_bool": ([True, False], False),
    "arrow_decimal128": ([decimal.Decimal("1.5"), decimal.Decimal("2.5")], False),
    "arrow_float64": ([1.5, 2.5], False),
}

# Extension dtypes must be constructed with an explicit dtype; the plain
# constructor would infer a numpy dtype and never exercise the box.
_ADJACENCY_POOL_DTYPES: dict[str, Any] = {
    "ext_boolean": "boolean",
    "ext_Int64": "Int64",
    "ext_Float64": "Float64",
    "ext_string": "string",
    "arrow_ts_s": pd.ArrowDtype(pa.timestamp("s")),
    "arrow_ts_s_overflow": pd.ArrowDtype(pa.timestamp("s")),
    "arrow_ts_us_overflow": pd.ArrowDtype(pa.timestamp("us")),
    "arrow_ts_ns": pd.ArrowDtype(pa.timestamp("ns")),
    "arrow_duration_s_overflow": pd.ArrowDtype(pa.duration("s")),
    "arrow_date32_overflow": pd.ArrowDtype(pa.date32()),
    "arrow_time64_us_overflow": pd.ArrowDtype(pa.time64("us")),
    "arrow_int64": pd.ArrowDtype(pa.int64()),
    "arrow_string": pd.ArrowDtype(pa.string()),
    "arrow_bool": pd.ArrowDtype(pa.bool_()),
    "arrow_decimal128": pd.ArrowDtype(pa.decimal128(20, 4)),
    "arrow_float64": pd.ArrowDtype(pa.float64()),
}

# Pools whose raw values only MEAN what the pool intends under the pool's
# own dtype. An arrow `timestamp[s]` pool holds the ints 0 and 1, which
# are two timestamps under that dtype and two plain integers under any
# other. Building the neighbour by re-inferring from a list therefore
# yields different DATA, not a reboxing of the same data, and comparing
# the two is meaningless. For these the list neighbour must be built
# under the same schema; the `pd.concat` axis is already correct, since
# it boxes the existing values rather than re-reading them.
_ADJACENCY_REINTERPRETING_POOLS = frozenset(
    {
        "arrow_ts_s",
        "arrow_ts_s_overflow",
        "arrow_ts_us_overflow",
        "arrow_ts_ns",
        "arrow_duration_s_overflow",
        "arrow_date32_overflow",
        "arrow_time64_us_overflow",
    }
)

# Pools that cannot reach a neighbour of a DIFFERENT dtype, so they carry
# recordwise coverage and contribute nothing to boxing invariance. dennis
# round 10 (MEDIUM-c) found two arrow pools silently in this state; the
# audit that followed found five more that had always been. Recorded as
# explicit decisions rather than silent zeros: the guard below fails both
# when an unexempted pool reaches no dtype change AND when an exempted one
# starts reaching them, so neither direction can drift unnoticed.
_NO_CROSS_BOXING_NEIGHBOUR = frozenset(
    {
        # `pd.concat` itself raises while widening these to object, because
        # the values sit so far outside the Python range, and their list
        # axis is pinned to their own schema because they reinterpret.
        "arrow_date32_overflow",
        "arrow_time64_us_overflow",
        # Already object, and object is the one dtype that cannot upcast:
        # every added row leaves it object. They carry recordwise coverage
        # and, deliberately, no boxing coverage.
        #
        # `string` is exempt only under the DEFAULT `future.infer_string`
        # (Codex round 11): with that option enabled the base infers as
        # `str` and an added numeric row widens it to `object`, so the pool
        # reaches 21 cross-boxing neighbours and the exemption is wrong.
        # The test below pins both halves of that so the dependency is
        # recorded rather than silently inherited from a global default.
        "string",
        "mixed_num_str",
        "huge_int_obj",
        "decimal_obj",
        "decimal_mixed",
    }
)

# Added rows chosen to trigger a dtype change on the pools above.
_ADJACENCY_ADDED: dict[str, object] = {
    "none": None,
    "nan": np.nan,
    "pd_na": pd.NA,
    "str": "x",
    "float": 1.5,
    "int": 7,
    "huge_int": 2**70,
    "bool": True,
    "timedelta": datetime.timedelta(days=9),
    "timestamp": pd.Timestamp("2021-06-01"),
    # dennis round 8: complex was absent despite being the one type this
    # module has a dedicated helper for. One complex row re-types a whole
    # numeric column to complex128.
    "complex_real": 3 + 0j,
    "complex_imag": 1 + 1j,
    # dennis round 10: fits in int64 (so the arrow pools accept it) but
    # leaves the Python datetime range, so on an arrow temporal pool the
    # neighbour differs by a cell whose FETCH raises rather than whose
    # conversion fails. `huge_int` above cannot reach this: 2**70
    # overflows int64 and the frame will not build at all.
    "int64_out_of_python_range": 2**60,
}


def _build_series(values: list, dtype: Any) -> pd.Series:
    """Build a pool or neighbour series under `dtype`.

    Codex round 10 (MEDIUM): `pd.Series(..., dtype=pd.ArrowDtype(...))`
    routes through pandas' own scalar coercion, which refuses several
    arrow types outright -- `duration[s]` raised `OutOfBoundsTimedelta`
    on an out-of-range value, so all 26 of its named cases reported
    PASSED while executing zero assertions. `read_parquet` hands back an
    arrow-native array that pandas never coerces, so build these the way
    the data actually arrives.
    """
    if isinstance(dtype, pd.ArrowDtype):
        return pd.Series(pd.arrays.ArrowExtensionArray(pa.array(values, type=dtype.pyarrow_dtype)))
    return pd.Series(values, dtype=dtype)


def _public_part(column_block: dict) -> dict:
    """The part of a column block that must not vary with row content."""
    public = {k: v for k, v in column_block.items() if k not in _RELEASED_COLUMN_KEYS}
    stats = public.get("stats")
    if isinstance(stats, dict):
        public["stats"] = {k: v for k, v in stats.items() if k not in _RELEASED_STATS_KEYS}
    return public


def _mixed_df(n: int = 2000, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "age": rng.integers(0, 120, size=n).astype(float),
            "state": rng.choice(["CA", "NY", "TX", "ZZ"], size=n, p=[0.5, 0.3, 0.15, 0.05]),
        }
    )


class TestConfigValidation:
    def test_production_dp_fit_exposes_no_seed_or_rng_parameter(self):
        """C-B1: the public entrypoint accepts no mechanism/backend
        injection parameter of ANY name, not merely the two literal names
        this test used to check. `_session_backend` (or any future
        differently-named seam) is a mechanism-substitution bypass on the
        public API if it reaches this signature -- Codex demonstrated a
        forged backend producing a plausible-looking artifact from exact
        source counts. This asserts the FULL parameter set is exactly the
        documented public contract, so a new injection parameter under any
        name fails this test the moment it's added here, not only if it
        happens to be named `seed` or `rng`."""
        import inspect

        # Introspect the REAL new-signature entrypoint (this suite's
        # `fit_dp_snapshot` is a legacy-kwargs translation shim). The phase-5
        # API is `fit_dp_snapshot(source, column_schema, *, epsilon, delta,
        # numeric_bins)`; it still exposes no mechanism/backend injection
        # parameter of any name.
        sig = inspect.signature(_real_fit_dp_snapshot)
        assert set(sig.parameters) == {
            "source",
            "column_schema",
            "epsilon",
            "delta",
            "numeric_bins",
        }

    def test_dp_fit_rejects_a_schema_column_absent_from_the_frame(self):
        """Phase-5 migration: the fit fits exactly the columns declared in
        `column_schema` (partial frame coverage is allowed -- a marginal release
        describes what it declares). A declared column that is NOT in the frame
        fails closed in the adapter with a coded `CarrierError`, before any cell
        is read, rather than silently releasing nothing."""
        df = pd.DataFrame({"age": [1.0, 2.0], "state": ["a", "b"]})
        with pytest.raises(CarrierError) as exc:
            _real_fit_dp_snapshot(
                df,
                {"missing": {"kind": "categorical", "carrier": "text"}},
                epsilon=1.0,
                delta=1e-6,
            )
        assert exc.value.code == "dp_adapter_missing_column"

    def test_dp_fit_rejects_an_unknown_carrier(self):
        """Phase-5 migration: a per-column carrier outside the closed set
        (`number`/`flag`/`text`) is rejected before any private cell is read.
        (The legacy `categorical + numeric` OVERLAP this test used to cover is
        structurally impossible now: `column_schema` keys are unique, so a
        column declares exactly one carrier by construction.)"""
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(CarrierError) as exc:
            _real_fit_dp_snapshot(
                df,
                {"age": {"carrier": "bogus", "bounds": (0.0, 120.0)}},
                epsilon=1.0,
                delta=1e-6,
            )
        assert exc.value.code == "dp_carrier_unknown"

    @pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan"), "abc"])
    def test_invalid_epsilon_rejected(self, bad):
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df,
                categorical_columns=[],
                numeric_domains={"age": (0.0, 120.0)},
                epsilon=bad,
                delta=1e-6,
            )
        assert exc.value.code == "dp_epsilon_invalid"

    @pytest.mark.parametrize("bad", [0, 1, 1.0, -1e-6, float("nan"), float("inf")])
    def test_invalid_delta_rejected_including_zero(self, bad):
        # delta=0 is rejected even for a numeric-only fit (guide 9.10 item 2).
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df,
                categorical_columns=[],
                numeric_domains={"age": (0.0, 120.0)},
                epsilon=1.0,
                delta=bad,
            )
        assert exc.value.code == "dp_delta_invalid"

    @pytest.mark.parametrize("bad", [1, 1.5, 0, -1])
    def test_invalid_numeric_bins_rejected(self, bad):
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df,
                categorical_columns=[],
                numeric_domains={"age": (0.0, 120.0)},
                epsilon=1.0,
                delta=1e-6,
                numeric_bins=bad,
            )
        assert exc.value.code == "dp_numeric_bins_invalid"

    @pytest.mark.parametrize(
        "bounds", [(120.0, 0.0), (0.0, 0.0), (float("nan"), 1.0), (0.0, float("inf"))]
    )
    def test_invalid_numeric_domain_rejected(self, bounds):
        # Phase-5 migration: number-carrier bounds are validated by the carrier
        # layer (`_validate_bound` + the order check), so a malformed/misordered
        # domain now fails closed with a coded `CarrierError` (nonfinite bound or
        # bad order) rather than the legacy `DpError('dp_numeric_domain_invalid')`.
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(CarrierError) as exc:
            fit_dp_snapshot(
                df, categorical_columns=[], numeric_domains={"age": bounds}, epsilon=1.0, delta=1e-6
            )
        assert exc.value.code in ("dp_carrier_bounds_order", "dp_carrier_bounds_nonfinite")

    @pytest.mark.parametrize(
        ("bounds", "bins"),
        [((0.0, 1.7e308), 10), ((1.0, 1.0000000000000002), 100)],
        ids=["overflowing_width", "collapsing_width"],
    )
    def test_domains_whose_derived_bin_edges_degenerate_raise_a_coded_error(self, bounds, bins):
        """D-M-B (dennis round 5): finite `lower < upper` passed
        validation, but the DERIVED interior edges could overflow or
        collapse to non-unique values, and OpenDP then rejected them at
        the FFI with a raw `OpenDPException`, after the row-count release
        had already charged the session. `(0.0, 1.7e308)` is a plausible
        "just give it a wide domain" input. This module documents that it
        raises coded `DpError`, so the edges are now derived from the
        public declaration and checked before any value is touched. Not a
        privacy channel: the outcome is a function of the declaration
        alone, identical for both neighbours."""
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                pd.DataFrame({"age": [1.0] * 5}),
                categorical_columns=[],
                numeric_domains={"age": bounds},
                epsilon=2.0,
                delta=1e-6,
                numeric_bins=bins,
            )
        assert exc.value.code == "dp_numeric_domain_invalid"

    @pytest.mark.parametrize(
        ("bounds", "bins"),
        [((0.0, 120.0), 10), ((0.0, 1.0), 2), ((-50.0, 50.0), 100), ((0.0, 1e-300), 10)],
        ids=["ordinary", "two_bins", "many_bins", "very_small"],
    )
    def test_ordinary_domains_still_fit(self, bounds, bins):
        """The edge guard must reject only degenerate declarations. An
        earlier revision of it used `zip(full, full[1:], strict=True)`,
        whose operands differ in length by one by construction, and so
        raised `ValueError` on EVERY fit; only including ordinary
        domains here caught that."""
        snap = fit_dp_snapshot(
            pd.DataFrame({"age": [1.0] * 5}),
            categorical_columns=[],
            numeric_domains={"age": bounds},
            epsilon=2.0,
            delta=1e-6,
            numeric_bins=bins,
        )
        assert len(snap["columns"]["age"]["stats"]["bin_counts"]) == bins

    # Phase-5 migration: the legacy `dp_column_label_not_a_string` and
    # `dp_column_label_not_a_string` (frame-side) rejections are removed. The
    # new API matches `column_schema` keys directly against the frame's column
    # labels (a schema column absent from the frame fails closed with
    # `dp_adapter_missing_column`, covered above), and the carrier layer no
    # longer stringifies declarations, so there is no str-coercion mismatch to
    # guard. Column-name typing is the adapter's concern and is covered by
    # test_carriers.py's adapter suite.

    def test_declarations_are_read_once_so_a_drifting_mapping_cannot_slip_past(self):
        """D-LOW-1 (dennis round 6): the declarations were read three
        times (validation, schedule construction, fit loop), so a
        `Mapping` whose reads differ passed every check and then handed
        OpenDP different bounds, landing a raw `OpenDPException` AFTER
        `release_row_count` had charged the session. They are snapshotted
        once at entry now, so later reads cannot diverge."""
        from collections.abc import Mapping

        class _Drifting(Mapping):
            def __init__(self):
                self.reads = 0

            def __getitem__(self, key):
                self.reads += 1
                return (0.0, 100.0) if self.reads <= 2 else (0.0, 1.7e308)

            def __iter__(self):
                return iter(["age"])

            def __len__(self):
                return 1

        drifting = _Drifting()
        snap = fit_dp_snapshot(
            pd.DataFrame({"age": [1.0] * 5}),
            categorical_columns=[],
            numeric_domains=drifting,
            epsilon=2.0,
            delta=1e-6,
        )
        # The snapshot pinned the first read, so the declared bounds are
        # what the artifact reports, not whatever the mapping drifted to.
        assert snap["columns"]["age"]["stats"]["bin_edges"][-1] == 100.0

    def test_duplicate_frame_column_labels_are_rejected(self):
        """A schema key that selects TWO frame columns (`df[name]` is a
        DataFrame, not a Series) is a structural, columns-level problem, not a
        row-value one. The adapter rejects it with a coded `CarrierError` before
        any cell is read, rather than silently releasing a distribution over the
        column label. (Phase-5 migration: this was `dp_column_declaration_
        duplicated` in the legacy fit; the carrier adapter owns it now.)"""
        df = pd.DataFrame([["CA", "NY"], ["TX", "CA"]], columns=["x", "x"])
        with pytest.raises(CarrierError) as exc:
            fit_dp_snapshot(
                df, categorical_columns=["x"], numeric_domains={}, epsilon=2.0, delta=1e-6
            )
        assert exc.value.code == "dp_adapter_duplicate_column"

    def test_numeric_bins_default_is_ten_and_recorded_in_artifact(self):
        df = pd.DataFrame({"age": [1.0, 2.0, 3.0]})
        snap = fit_dp_snapshot(
            df,
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=1.0,
            delta=1e-6,
        )
        assert snap["dp"]["numeric_bins"] == 10
        assert len(snap["columns"]["age"]["stats"]["bin_counts"]) == 10


class TestDpFitIsIndependentOfExactSnapshot:
    """D-M4/D-M5 (dennis must-fix): `fit_dp_snapshot` and `compute_
    distribution_snapshot` (the pre-existing NON-DP fit) are fully
    independent code paths (guide section 5 step 3: "Do not route the DP
    fit through compute_distribution_snapshot... it materializes exact
    statistics and has value-dependent kind behavior unsuitable for
    DP"). These name and pin that independence directly, rather than
    leaving it as an unstated property of the two modules never
    importing each other."""

    def test_dp_fit_does_not_call_compute_distribution_snapshot(self, monkeypatch):
        """D-M5: patches `compute_distribution_snapshot` to raise if
        called at all, then runs a full mixed-column DP fit. If a future
        change ever routed the DP path through the exact snapshot
        builder -- the direct disclosure the guide warns against -- this
        fails immediately."""
        import decoy_engine.quality.snapshot as snapshot_module

        def _boom(*args, **kwargs):
            raise AssertionError("fit_dp_snapshot must never call compute_distribution_snapshot")

        monkeypatch.setattr(snapshot_module, "compute_distribution_snapshot", _boom)
        fit_dp_snapshot(
            _mixed_df(),
            categorical_columns=["state"],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=2.0,
            delta=1e-6,
        )  # must not raise

    def test_non_dp_snapshot_behavior_is_unchanged_after_dp_fit_split(self):
        """D-M4: calling `fit_dp_snapshot` must not change `compute_
        distribution_snapshot`'s own output for an identical input -- no
        shared mutable state, no import-time side effect that alters
        non-DP behavior. Calls the exact (non-DP) builder before and
        after a DP fit and asserts byte-identical results."""
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = pd.DataFrame({"age": [1.0, 2.0, 3.0, None], "state": ["CA", "NY", "CA", None]})
        before = compute_distribution_snapshot(df)
        fit_dp_snapshot(
            _mixed_df(),
            categorical_columns=["state"],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=2.0,
            delta=1e-6,
        )
        after = compute_distribution_snapshot(df)
        assert before == after

    def test_private_values_cannot_change_declared_dp_kind(self):
        """D-M6 (binding decision 5): kind comes EXCLUSIVELY from the
        caller's declaration, never inferred from dtype, values, or
        cardinality. A column declared categorical whose every value
        LOOKS numeric (parseable as a float) still emits `kind:
        categorical` -- there is no runtime branch that could read
        the declaration differently based on content shape."""
        df = pd.DataFrame({"code": ["1", "2", "3", "1", "2"]})
        snap = fit_dp_snapshot(
            df, categorical_columns=["code"], numeric_domains={}, epsilon=2.0, delta=1e-6
        )
        assert snap["columns"]["code"]["kind"] == "categorical"
        assert "bin_edges" not in snap["columns"]["code"]["stats"]


class TestRecordwiseNormalization:
    """D-M6/C-B2: the one stability claim Decoy makes on its own (guide
    section 3.3 item 1) is that preprocessing is recordwise -- every input
    row contributes AT MOST one element to a column's normalized vector.
    Pinned directly at the `_normalize_numeric`/`_normalize_categorical`
    seam, independent of the OpenDP mechanism."""

    def test_normalize_numeric_output_length_equals_non_null_input_length(self):
        series = pd.Series([1.0, None, 3.0, float("nan"), 5.0])
        out = _normalize_numeric(series, lower=0.0, upper=10.0)
        assert len(out) == 3  # 1.0, 3.0, 5.0 -- None and NaN excluded

    def test_normalize_numeric_removing_one_row_removes_at_most_one_element(self):
        base = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        base_out = _normalize_numeric(base, lower=0.0, upper=10.0)
        for i in range(len(base)):
            dropped = base.drop(base.index[i]).reset_index(drop=True)
            dropped_out = _normalize_numeric(dropped, lower=0.0, upper=10.0)
            assert _multiset_distance(base_out, dropped_out) <= 1

    def test_normalize_categorical_output_length_equals_non_null_input_length(self):
        series = pd.Series(["a", None, "b", None, "c"])
        out = _normalize_categorical(series)
        assert len(out) == 3

    def test_normalize_categorical_removing_one_row_removes_at_most_one_element(self):
        base = pd.Series(["a", "b", "c", "d", "e"])
        base_out = _normalize_categorical(base)
        for i in range(len(base)):
            dropped = base.drop(base.index[i]).reset_index(drop=True)
            dropped_out = _normalize_categorical(dropped)
            assert _multiset_distance(base_out, dropped_out) <= 1

    def test_the_string_pool_exemption_depends_on_a_pandas_global(self):
        """Codex round 11: `string`'s exemption reads as a property of the
        data, and it is really a property of `pd.options.future.infer_string`.

        Under the default the pool is object dtype and nothing widens it.
        Enable the option and the base infers as `str`, an added numeric row
        widens to `object`, and the exemption is simply wrong -- so when that
        option becomes the default, the exemption must go. Pin both halves
        now rather than discover it as a silent coverage loss then."""
        values = ["a", "b"]
        original = pd.options.future.infer_string
        try:
            pd.options.future.infer_string = False
            assert pd.Series(values).dtype == object
            assert pd.Series([*values, 7]).dtype == object  # no widening: exemption holds

            pd.options.future.infer_string = True
            widened = pd.Series(values)
            assert widened.dtype != object  # infers as `str`
            assert pd.Series([*values, 7]).dtype != widened.dtype  # widening IS reachable
        finally:
            pd.options.future.infer_string = original

    @pytest.mark.parametrize("pool_id", sorted(_ADJACENCY_POOLS), ids=sorted(_ADJACENCY_POOLS))
    def test_every_declared_pool_actually_produces_a_comparison(self, pool_id):
        """Codex round 10 (MEDIUM): a pool whose construction raises is
        silently skipped by both matrix tests, so it reports PASSED on
        every one of its named cases while executing zero assertions.
        `arrow_duration_s_overflow` did exactly that for all 26 of its
        cases, and the only reason it was noticed was a second model
        counting them.

        A skip on a SPECIFIC added row is legitimate -- pandas genuinely
        refuses some combinations. A pool that never builds at all, or
        that never yields a single neighbour across the whole added-row
        axis, is a false green and must fail loudly."""
        pool, obj = _ADJACENCY_POOLS[pool_id]
        pool_dtype = _ADJACENCY_POOL_DTYPES.get(pool_id)

        built = 0
        compared = 0
        cross_boxing = 0
        for dtype in (
            (object,) if (obj or pool_id in _ADJACENCY_REINTERPRETING_POOLS) else (object, None)
        ):
            try:
                base = _build_series(list(pool), pool_dtype or dtype)
            except (OverflowError, ValueError, TypeError, pa.ArrowException):
                continue
            built += 1
            list_dtype = pool_dtype if pool_id in _ADJACENCY_REINTERPRETING_POOLS else dtype
            for added in _ADJACENCY_ADDED.values():
                for make in ("list", "concat"):
                    try:
                        neighbour = (
                            _build_series([*pool, added], list_dtype)
                            if make == "list"
                            else pd.concat([base, pd.Series([added])], ignore_index=True)
                        )
                    except (OverflowError, ValueError, TypeError, pa.ArrowException):
                        continue
                    compared += 1
                    if neighbour.dtype != base.dtype:
                        cross_boxing += 1

        assert built, f"{pool_id}: the pool itself never builds; every one of its cases is vacuous"
        assert compared, f"{pool_id}: builds but yields no neighbour on any added row; vacuous"
        if pool_id in _NO_CROSS_BOXING_NEIGHBOUR:
            assert not cross_boxing, (
                f"{pool_id} is exempted from the cross-boxing requirement but now reaches "
                f"{cross_boxing} such neighbours; drop the exemption"
            )
        else:
            assert cross_boxing, (
                f"{pool_id}: every neighbour has the base's own dtype, so it exercises the "
                "fetch guard but contributes nothing to boxing invariance, which is what "
                "this matrix exists to prove. Either give it a reachable dtype change or "
                "add it to _NO_CROSS_BOXING_NEIGHBOUR with the reason."
            )

    @pytest.mark.parametrize("pool_id", sorted(_ADJACENCY_POOLS), ids=sorted(_ADJACENCY_POOLS))
    @pytest.mark.parametrize("added_id", sorted(_ADJACENCY_ADDED), ids=sorted(_ADJACENCY_ADDED))
    def test_categorical_normalization_is_multiset_recordwise(self, pool_id, added_id):
        """The property `map(1)` actually assumes, stated as a MULTISET
        bound over genuine one-row neighbours, across a dtype matrix.

        dennis round 7 (HIGH-1): the two tests carrying this property's
        name compared CARDINALITY only. Both neighbours had length 3
        while every element differed, so they passed while the guarantee
        was broken -- the structural reason this defect class recurred
        twice. A length bound is not the property; the composed
        (epsilon, delta) is only sound if the normalized MULTISETS differ
        by at most one element.

        The matrix is the point. The pools straddle float64's exact
        integer range in both signs and cross `str`/`bool`/extension/
        unsupported types; the added rows are the values that trigger a
        dtype change (null, NaN, an incompatible string, a float, a huge
        int, a complex). Neither construction forces `dtype=object`,
        because that is the one dtype that cannot upcast -- forcing it is
        exactly how the sibling test below passed with the coercion
        removed.

        BOTH construction axes are required (dennis round 8). Building
        the neighbour as `pd.Series([*pool, added])` cannot produce
        bool->int64, bool->float, or real->complex128 boxing at all: list
        construction re-infers from scratch, while `pd.concat` boxes the
        EXISTING values into the combined dtype, which is what a
        partitioned or chunked read actually does. The list-only matrix
        reported zero violations on four separate guarantee breaks."""
        pool, obj = _ADJACENCY_POOLS[pool_id]
        added = _ADJACENCY_ADDED[added_id]
        pool_dtype = _ADJACENCY_POOL_DTYPES.get(pool_id)

        # The two passes are only degenerate for a REINTERPRETING pool,
        # where `list_dtype = pool_dtype` regardless of `dtype`. For any
        # other pinned-dtype pool they build DIFFERENT list neighbours --
        # `object` in one pass, re-inferred in the other -- and collapsing
        # the axis on `pool_dtype` deleted 279 of 942 cross-boxing
        # comparisons (dennis round 11, HIGH-1). What went with them:
        # `ext_boolean`/`arrow_bool` -> numpy `bool`, which is the
        # `np.bool_` transition round 8 added that pool FOR, and
        # `ext_Int64`/`ext_Float64`/`arrow_int64`/`arrow_float64` ->
        # `complex128`, which is round 8's real-to-complex re-typing.
        # The cross-boxing guard did not catch it because ("list",
        # "object") still differs from the base dtype.
        for dtype in (
            (object,) if (obj or pool_id in _ADJACENCY_REINTERPRETING_POOLS) else (object, None)
        ):
            build_dtype = pool_dtype or dtype
            try:
                base = _build_series(list(pool), build_dtype)
            except (OverflowError, ValueError, TypeError, pa.ArrowException):
                continue  # pandas itself refuses the pool
            base_labels = _normalize_categorical(base)

            neighbours = []
            list_dtype = pool_dtype if pool_id in _ADJACENCY_REINTERPRETING_POOLS else dtype
            try:
                neighbours.append(_build_series([*pool, added], list_dtype))
            except (OverflowError, ValueError, TypeError, pa.ArrowException):
                pass
            try:  # concat: boxes the EXISTING values into the joint dtype
                neighbours.append(pd.concat([base, pd.Series([added])], ignore_index=True))
            except (OverflowError, ValueError, TypeError, pa.ArrowException):
                pass
            if not neighbours:
                continue

            for neighbour in neighbours:
                assert _multiset_distance(base_labels, _normalize_categorical(neighbour)) <= 1, (
                    f"{pool_id} + {added_id}: {base.dtype} -> {neighbour.dtype}"
                )

    @pytest.mark.parametrize("n", [8, 40], ids=["small", "wide"])
    @pytest.mark.parametrize(
        "base_value",
        [0, 2**53 - 4, 2**60, -(2**60)],
        ids=["small", "at_float64_limit", "beyond_float64", "negative_beyond"],
    )
    def test_categorical_labels_are_stable_when_a_null_upcasts_the_column(self, n, base_value):
        """Codex round 6, the one defect in this program that broke the
        DP guarantee itself rather than a test or an error contract.

        pandas upcasts an integer column to float64 the moment a null
        enters it, so under plain `str()` EVERY label changed when ONE
        row was added:

            ints 0..7            -> ["0" ... "7"]
            ints 0..7 and a null -> ["0.0" ... "7.0"]

        The normalized multisets differed by 2n elements while the
        grouped-count measurement certifies `map(1)`, so the composed
        `(epsilon, delta)` understated the true sensitivity by a factor
        of the column's cardinality. Normalization was recordwise GIVEN A
        FRAME, but a frame's storage dtype is a function of ALL its rows.

        Every recordwise test before this one used STRING fixtures, which
        never upcast, which is why five review rounds missed it. Integer
        fixtures are the point here.

        dennis round 7 (HIGH-2) added the MAGNITUDE axis. `range(n)` is
        exactly the range where the round-6 fix could not fail: those
        values survive the float64 round-trip, so `str(int(raw))` and
        `str(int(float(raw)))` agree. Beyond 2**53 they do not, and the
        released distribution collapsed to a single label. The
        `beyond_float64` ids are the falsifier; `small` still pins the
        original round-6 regression."""
        values = [base_value + 2 * i for i in range(n)]
        base = pd.Series(values)
        neighbour = pd.Series([*values, None])
        assert base.dtype != neighbour.dtype  # the upcast this guards against
        base_labels = _normalize_categorical(base)
        neighbour_labels = _normalize_categorical(neighbour)
        # Adding one null row adds no label and changes none of the rest.
        assert base_labels == neighbour_labels
        assert _multiset_distance(base_labels, neighbour_labels) == 0

    def test_canonical_label_merges_integral_reals_but_preserves_everything_else(self):
        """An integral real renders as its integer string whatever its
        storage width, so the upcast above cannot move it, and a
        non-finite float renders from its float64 image, so `inf` stays
        "inf".

        `bool` renders "1"/"0" and NOT "True"/"False" (dennis round 8).
        The round-7 version of this test pinned "True"/"False" and the
        round-7 docstring defended it, both backwards: a bool column's
        float64 image is 1.0/0.0, and pandas re-types bool to int64 or
        float64 on ordinary neighbours, so "True"/"False" moved 1600
        labels on an 800-row column while "1"/"0" does not move at all.
        The resulting `True`/`1` collision is a coarsening and can only
        weaken a release, which is the disposition already accepted for
        `7`/`7.0`."""
        series = pd.Series(
            [7, 7.0, np.int64(7), np.float64(7.0), 7.5, True, False, "CA", float("inf")],
            dtype=object,
        )
        assert _normalize_categorical(series) == [
            "7",
            "7",
            "7",
            "7",
            "7.5",
            "1",
            "0",
            "CA",
            "inf",
        ]

    @pytest.mark.parametrize("pool_id", sorted(_ADJACENCY_POOLS), ids=sorted(_ADJACENCY_POOLS))
    @pytest.mark.parametrize("added_id", sorted(_ADJACENCY_ADDED), ids=sorted(_ADJACENCY_ADDED))
    def test_numeric_normalization_is_multiset_recordwise(self, pool_id, added_id):
        """The same matrix against `_normalize_numeric`.

        dennis round 8 (HIGH-1, item 4): the numeric path had NO adjacency
        matrix -- only the two pre-existing drop-one-row loops over a
        fixed float fixture. That is how BLOCKER-4 survived in the very
        code C-B2 claimed to have closed: one complex row re-typed the
        column to complex128, every previously-real value arrived boxed
        as complex and was dropped as "unconvertible", and the released
        bin_counts went from two populated bins to all zeros."""
        pool, obj = _ADJACENCY_POOLS[pool_id]
        added = _ADJACENCY_ADDED[added_id]
        pool_dtype = _ADJACENCY_POOL_DTYPES.get(pool_id)
        bounds = {"lower": -(2.0**62), "upper": 2.0**62}

        for dtype in (
            (object,) if (obj or pool_id in _ADJACENCY_REINTERPRETING_POOLS) else (object, None)
        ):
            try:
                base = _build_series(list(pool), pool_dtype or dtype)
            except (OverflowError, ValueError, TypeError):
                continue
            base_out = _normalize_numeric(base, **bounds)

            neighbours = []
            list_dtype = pool_dtype if pool_id in _ADJACENCY_REINTERPRETING_POOLS else dtype
            try:
                neighbours.append(_build_series([*pool, added], list_dtype))
            except (OverflowError, ValueError, TypeError, pa.ArrowException):
                pass
            try:  # concat: boxes the EXISTING values into the joint dtype
                neighbours.append(pd.concat([base, pd.Series([added])], ignore_index=True))
            except (OverflowError, ValueError, TypeError, pa.ArrowException):
                pass

            for neighbour in neighbours:
                assert _multiset_distance(base_out, _normalize_numeric(neighbour, **bounds)) <= 1, (
                    f"{pool_id} + {added_id}: {base.dtype} -> {neighbour.dtype}"
                )

    def test_integers_too_large_for_float64_are_labelled_not_dropped(self):
        """Routing reals through their float64 image means `float(raw)`
        can raise `OverflowError` for a big enough integer. Falling back
        to the exact integer string is sound rather than a special case:
        such a value cannot live in an `int64` column, so pandas holds it
        in `object` dtype, which never upcasts, so nothing can move the
        label.

        Without the fallback these rows drop instead. That is not a
        guarantee break -- dropping stays recordwise -- which is exactly
        why it needs its own test: the adjacency matrix passes either
        way, and dennis round 7 called the silent loss out specifically."""
        huge = [10**400, 10**401]
        series = pd.Series(huge, dtype=object)
        assert _normalize_categorical(series) == [str(v) for v in huge]

    def test_datetimelike_values_are_rejected_at_every_resolution(self):
        """Round 9, both reviewers: `.item()` is type-ERASING.

        `np.datetime64(...,"ns").item()` returns an `int` (epoch
        nanoseconds), and an `int` passes the real-number gate, so a date
        column was labelled with its raw epoch integers -- releasing the
        timestamps themselves in a privacy product -- while the same
        values boxed as `pandas.Timestamp` were correctly dropped. The
        numeric path broke independently, because
        `float(np.datetime64(...,"ns"))` also succeeds.

        Resolution is the axis, so it is the axis under test. `s` and
        `us` unbox to `datetime.datetime` and were always rejected; only
        `ns`, which is pandas' native resolution, unboxed to `int`."""
        for unit in ("s", "ms", "us", "ns"):
            stamps = pd.Series([np.datetime64(f"2020-01-0{1 + i % 3}", unit) for i in range(30)])
            deltas = pd.Series([np.timedelta64(i % 3, unit) for i in range(30)])
            for series in (stamps, deltas):
                assert _normalize_categorical(series) == [], f"labelled at {unit}"
                assert _normalize_numeric(series, lower=0.0, upper=1e20) == [], (
                    f"converted at {unit}"
                )

        # The pandas and Python boxes of the same quantities, which were
        # always rejected -- the disposition must not depend on the box.
        boxed = pd.Series(
            [pd.Timestamp("2020-01-01"), pd.Timedelta(1, "D"), datetime.date(2020, 1, 1)],
            dtype=object,
        )
        assert _normalize_categorical(boxed) == []

    def test_complex_is_labelled_only_when_its_imaginary_part_is_zero(self):
        """dennis round 9 (MEDIUM-1): a surviving mutant.

        Replacing `if raw.imag != 0:` with `if False:` -- so a genuinely
        complex value gets its real part labelled instead of dropped --
        passed all 639 tests. The numeric twin died on the matrix; the
        categorical side had pure coverage asymmetry. Both directions are
        pinned here in one place."""
        series = pd.Series([1 + 0j, 2 + 0j, 3 + 4j], dtype=object)
        assert _normalize_categorical(series) == ["1", "2"]
        assert _normalize_numeric(series, lower=0.0, upper=10.0) == [1.0, 2.0]

    def test_decimal_is_treated_the_same_by_both_normalizers(self):
        """dennis round 9 (MEDIUM-2): `Decimal` is `numbers.Number` but
        not `numbers.Real`, so the categorical gate dropped every one
        while the numeric path converted them happily -- two normalizers
        disagreeing about the same value on an ABC-registration accident
        rather than any boxing argument. `float(Decimal)` is exactly the
        float64-image rule already in use."""
        series = pd.Series([decimal.Decimal("1.5"), decimal.Decimal("2")], dtype=object)
        assert _normalize_categorical(series) == ["1.5", "2"]
        assert _normalize_numeric(series, lower=0.0, upper=10.0) == [1.5, 2.0]

    def test_the_policy_log_fires_unconditionally(self, caplog):
        """dennis round 9: mutation M9 made the logger inert and survived
        all 639 tests, because nothing referenced it.

        The message must fire on EVERY fit and must not vary with
        content: a record emitted only when a drop occurred is a
        probability-0-vs-1 observable, which is what round 9 blocked."""
        clean = pd.DataFrame({"c": ["a", "b"] * 100})
        dropped = pd.DataFrame({"c": [pd.Timestamp("2020-01-01")] * 200})
        records = []
        for df in (clean, dropped):
            caplog.clear()
            with caplog.at_level(logging.INFO, logger=_POLICY_LOGGER_NAME):
                fit_dp_snapshot(
                    df, categorical_columns=["c"], numeric_domains={}, epsilon=2.0, delta=1e-6
                )
            matching = [r for r in caplog.records if "dp fit:" in r.getMessage()]
            # dennis + Codex round 11 (both found this independently): the
            # `dp_policy` extraction moved the emitting logger out of
            # `decoy_engine.quality.dp`, and nothing noticed. The `logger=`
            # argument here was INERT -- `caplog.at_level` force-enables the
            # root logger, so the test passed while naming a logger that
            # emits nothing, and an operator filtering on the old name lost
            # the line silently. Assert the name on the record itself.
            assert {r.name for r in matching} == {_POLICY_LOGGER_NAME}, (
                f"policy line came from {sorted({r.name for r in matching})}, "
                f"not {_POLICY_LOGGER_NAME}"
            )
            emitted = [r.getMessage() for r in matching]
            assert emitted, "the policy line must fire on every fit"
            records.append(emitted)
        assert records[0] == records[1], "the log must not vary with frame content"

    def test_normalization_policy_is_identical_whatever_the_frame_holds(self):
        """The artifact says what normalization releases, in fixed bytes.

        Codex round 8 (MEDIUM): an all-date column releases as if
        all-null with nothing explaining why, so the artifact should
        carry the policy. dennis round 8 (MEDIUM-1) supplied the
        constraint: the policy may be stated but the per-column drop
        COUNT may not, because the count is an unnoised function of the
        data. The count goes to the log, where it discloses nothing --
        the party running the fit already holds the raw frame.

        So this asserts the policy does NOT vary with content. A policy
        that differed between these two frames would be exactly the
        channel the drop accounting exists to avoid."""
        supported = pd.DataFrame({"c": ["a", "b"] * 100})
        unsupported = pd.DataFrame({"c": [decimal.Decimal("1.5")] * 200})
        blocks = [
            fit_dp_snapshot(
                df, categorical_columns=["c"], numeric_domains={}, epsilon=2.0, delta=1e-6
            )["dp"]["normalization_policy"]
            for df in (supported, unsupported)
        ]
        assert blocks[0] == blocks[1]
        assert json.dumps(blocks[0], sort_keys=True) == json.dumps(blocks[1], sort_keys=True)

        # Codex round 9 (MEDIUM): content-independence is not
        # correctness. Replacing the whole policy with
        # {"categorical_labels": "everything is retained", ...} passed
        # all 507 focused tests, because nothing asserted what it SAYS.
        # Pin the exact dict, and assert each behaviour it claims.
        # Pin the LITERAL text, not the constant the artifact is built
        # from: `blocks[0] == _DP_NORMALIZATION_POLICY` compares the
        # value to itself, so mutating the constant moves both sides and
        # the assertion holds. That tautology let the garbage-policy
        # mutant survive a second time, in the fix for it.
        # dennis round 10 (LOW-1): the pinned text was itself inaccurate
        # on three points -- text is NOT kept verbatim when it carries a
        # NUL or cannot be encoded, "beyond float64 range" is exact only
        # up to the interpreter's decimal-conversion limit, and the
        # numeric line said "non-finite clamped" while NaN is dropped and
        # FINITE out-of-domain values are clamped too. A policy that
        # ships in every artifact has to be true.
        assert blocks[0] == {
            "categorical_labels": (
                "text kept verbatim unless the value AS RECEIVED contains NUL or cannot be "
                "encoded as UTF-8, noting that numpy fixed-width string storage strips a "
                "trailing NUL before the fit sees it; "
                "boolean, real, decimal and zero-imaginary complex rendered from the float64 "
                "image; an integer or rational too large for float64 rendered by its own exact "
                "string form instead, up to the interpreter's decimal-conversion limit; a decimal or "
                "extended-precision real too large for float64 released as the infinity its "
                "float64 image becomes; NaN released as null"
            ),
            "categorical_unsupported": (
                "released as null (datetime, timedelta, text whose value AS RECEIVED carries "
                "NUL or is not UTF-8 encodable, and any other type)"
            ),
            "numeric_values": (
                "float64, values outside the declared domain clamped to it, "
                "infinities clamped to the nearer bound, NaN released as null"
            ),
            # Codex round 13 (MEDIUM): the numeric policy documented clamping,
            # infinities and NaN but not that containers, datetimelikes,
            # nonzero-imaginary complex and any conversion failure become null
            # -- an incomplete disclosure in a dict that ships in every
            # artifact. Named now, and each path asserted just below.
            "numeric_unsupported": (
                "released as null (a list, tuple or array cell; a datetime or "
                "timedelta; a complex value with a nonzero imaginary part; and any "
                "value that cannot be converted to a float)"
            ),
        }
        # Each numeric-unsupported path the policy now names drops to null.
        assert _normalize_numeric(pd.Series([[1]], dtype=object), lower=0.0, upper=10.0) == []
        assert (
            _normalize_numeric(
                pd.Series([pd.Timestamp("2020-01-01")], dtype=object), lower=0.0, upper=10.0
            )
            == []
        )
        assert _normalize_numeric(pd.Series([1 + 2j], dtype=object), lower=0.0, upper=10.0) == []
        assert _normalize_numeric(pd.Series([object()], dtype=object), lower=0.0, upper=10.0) == []
        assert _normalize_categorical(pd.Series(["a"], dtype=object)) == ["a"]  # text verbatim
        assert _normalize_categorical(pd.Series([True], dtype=object)) == ["1"]  # bool -> image
        assert _normalize_categorical(pd.Series([decimal.Decimal("1.5")], dtype=object)) == ["1.5"]
        assert _normalize_categorical(pd.Series([1 + 0j], dtype=object)) == ["1"]  # zero-imaginary
        assert _normalize_categorical(pd.Series([10**400], dtype=object)) == [str(10**400)]  # exact
        assert _normalize_categorical(pd.Series([pd.Timestamp("2020-01-01")], dtype=object)) == []
        # Each exception the corrected text now claims.
        assert _normalize_categorical(pd.Series(["a\x00b"], dtype=object)) == []
        assert _normalize_categorical(pd.Series(["\ud800"], dtype=object)) == []
        assert _normalize_categorical(pd.Series([10**5000], dtype=object)) == []
        # Codex round 10 (LOW): the overflow fallback is not integer-only.
        # It fires for ANY accepted real whose float() overflows, and
        # renders that value's own exact repr -- a Fraction stays a
        # fraction, which the previous "integers ... rendered exactly"
        # wording denied.
        assert _normalize_categorical(pd.Series([Fraction(10**400, 3)], dtype=object)) == [
            str(Fraction(10**400, 3))
        ]
        # dennis + Codex round 11: the exact-repr branch is `except
        # OverflowError`, so only types whose `__float__` RAISES reach it.
        # Decimal and longdouble return inf silently and fall through to the
        # float64 image, which the previous "a real too large for float64"
        # wording denied -- and which also collides with a genuine infinity.
        assert _normalize_categorical(pd.Series([decimal.Decimal("1E+400")], dtype=object)) == [
            "inf"
        ]
        assert _normalize_categorical(pd.Series([np.longdouble("1e400")], dtype=object)) == ["inf"]
        assert _normalize_numeric(pd.Series([float("nan")]), lower=0.0, upper=10.0) == []
        assert _normalize_numeric(pd.Series([99.0]), lower=0.0, upper=10.0) == [10.0]
        assert _normalize_numeric(pd.Series([float("-inf")]), lower=0.0, upper=10.0) == [0.0]

    def test_nul_bearing_labels_drop_rather_than_silently_merging(self):
        """dennis round 10 (MEDIUM-1): a label is truncated at its first
        NUL when it crosses into OpenDP, so three distinct source values
        released as one, and the artifact asserted a `top_values` entry
        that was not a value in the source.

        Not a DP break -- a many-to-one label map is recordwise and
        boxing-invariant, so it can only coarsen -- but a silent
        truncation at the release boundary, which the repo does not
        permit. The uniform disposition is to drop."""
        series = pd.Series(["a\x00b"] * 3 + ["a"] * 3 + ["a\x00c"] * 3 + ["keep"], dtype=object)
        assert _normalize_categorical(series) == ["a"] * 3 + ["keep"]

    @pytest.mark.parametrize(
        "backing",
        ["object", "string", "string[pyarrow]", "arrow", "category", "numpy_U"],
    )
    def test_the_nul_disposition_is_recorded_for_every_string_backing(self, backing):
        """dennis round 10 (MEDIUM-a): the regression above covers only
        `dtype=object`, and the drop is NOT uniform across backings.

        numpy's fixed-width `U` dtype strips trailing NULs AT STORAGE, so
        `"a\x00"` reaches the normalizer as `"a"` -- indistinguishable from
        a genuine `"a"`, with the information destroyed before the fit sees
        the frame. We cannot make that uniform; we can only be accurate
        about it, which is why the policy string says "as received".

        Checked rather than assumed: this is not a DP break. Trailing-NUL
        stripping is irreversible and no pandas operation moves an object
        column into `U`, so no one-row neighbour can flip a column between
        the two dispositions. INTERIOR NUL drops everywhere, which is the
        case that actually merged distinct values.

        Pinned per backing so the divergence is on the record rather than
        latent, and so a future change to either side shows up here."""
        trailing, interior, plain = "a\x00", "a\x00b", "ab"
        values = [trailing, interior, plain]
        if backing == "numpy_U":
            series = pd.Series(np.array(values))
        elif backing == "arrow":
            series = pd.Series(pd.arrays.ArrowExtensionArray(pa.array(values)))
        elif backing == "category":
            series = pd.Series(values, dtype="category")
        else:
            series = pd.Series(values, dtype=backing)

        labels = _normalize_categorical(series)

        # Interior NUL always drops; plain text always survives.
        assert interior not in labels
        assert plain in labels
        if backing == "numpy_U":
            # Already truncated to "a" before we saw it, so it survives as
            # ordinary text. This asymmetry is deliberate and disclosed.
            assert labels == ["a", plain]
        else:
            assert labels == [plain]

    def test_unlabellable_types_drop_rather_than_raise(self):
        """Codex round 7 named timedelta and datetime columns as moving
        every label across a coercion: a `timedelta64` column labels
        "1 days 00:00:00", and one incompatible row forces `object`,
        where the same value labels "1 day, 0:00:00".

        They are dropped rather than labelled, and dropped rather than
        REJECTED. Dropping is the only disposition that is both total and
        coercion-invariant: the value drops identically under either
        boxing, so no added row can move a surviving label. Raising would
        reopen the fit-success channel C-B4 closed -- an all-string frame
        would succeed where its one-row neighbour carrying a timedelta
        raised, a probability-0-vs-1 observable that breaks
        (epsilon, delta) for any delta < 1 before a single released
        number is considered."""
        mixed = pd.Series(
            ["a", datetime.timedelta(days=1), pd.Timestamp("2020-01-01"), decimal.Decimal("1.5")],
            dtype=object,
        )
        # `Decimal` survives since round 9 (dennis MEDIUM-2): it has a
        # float64 image like any other number, and dropping it was an
        # ABC-registration accident rather than a boxing argument.
        assert _normalize_categorical(mixed) == ["a", "1.5"]  # total: no raise

        # The coercion Codex demonstrated, now invariant because both
        # boxings drop.
        deltas = [datetime.timedelta(days=1), datetime.timedelta(days=2)]
        native = pd.Series(deltas)
        coerced = pd.Series([*deltas, "x"])
        assert native.dtype != coerced.dtype  # timedelta64 -> object
        assert (
            _multiset_distance(_normalize_categorical(native), _normalize_categorical(coerced)) <= 1
        )

    def test_normalize_numeric_never_warns_on_exotic_content(self):
        series = pd.Series(["1", 1 + 2j, None, object(), "not a number"], dtype=object)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _normalize_numeric(series, lower=0.0, upper=10.0)
        assert caught == [], [str(w.message) for w in caught]

    @pytest.mark.parametrize(
        "normalize",
        [
            lambda s: _normalize_numeric(s, lower=0.0, upper=10.0),
            _normalize_categorical,
        ],
        ids=["numeric", "categorical"],
    )
    def test_no_warning_escapes_when_the_conversion_itself_warns(self, normalize):
        """D-H1: deleting either blanket suppression used to leave every
        test green, because the only warning-capable probe in the suite
        was a builtin `complex`, which the complex guard drops before
        `float()` runs. This probes the invariant the suppression
        actually claims -- no warning escapes regardless of row content
        -- with a value that warns during conversion itself. Removing
        either `simplefilter("ignore")` lets the warning through and
        fails this."""
        series = pd.Series(["1", _WarnsOnConversion(), "3"], dtype=object)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            normalize(series)
        assert caught == [], [str(w.message) for w in caught]

    def test_infinities_clamp_to_the_declared_bounds_rather_than_dropping(self):
        """D-M2: the documented `+-inf` behaviour (clamp to the declared
        bound) had no assertion, so replacing the clamp with a drop left
        every targeted test green. Fidelity rather than privacy, since
        either behaviour is recordwise, but it is a stated contract."""
        series = pd.Series([float("inf"), float("-inf")], dtype=object)
        assert _normalize_numeric(series, lower=0.0, upper=10.0) == [10.0, 0.0]

    def test_normalization_is_total_over_scalars_that_raise_on_conversion(self):
        """C-B4: normalization must not raise on ANY row content. Each
        hostile scalar below raises a DIFFERENT exception type, so a
        handler that enumerates conversion errors it thought of (the
        `(TypeError, ValueError)` this replaced) fails here rather than
        passing on the one type it happens to name."""
        for hostile in _HOSTILE_SCALARS:
            series = pd.Series(["1", hostile, "3"], dtype=object)
            numeric = _normalize_numeric(series, lower=0.0, upper=10.0)
            categorical = _normalize_categorical(series)
            # Dropped like any other unconvertible value: the two
            # convertible rows survive, the hostile row contributes none.
            assert numeric == [1.0, 3.0], (hostile, numeric)
            assert categorical == ["1", "3"], (hostile, categorical)

    def test_categorical_labels_that_cannot_be_encoded_are_dropped_not_raised(self):
        """C-B4, second location: a lone surrogate is a valid Python
        `str`, so `str()` succeeds and normalization used to pass it
        through. It then raised `UnicodeEncodeError` where the label
        crossed into OpenDP -- the same fit-success channel one layer
        down. Totality has to hold against the release boundary."""
        series = pd.Series(["a", "\ud800", "b"], dtype=object)
        assert _normalize_categorical(series) == ["a", "b"]
        snap = fit_dp_snapshot(
            pd.DataFrame({"c": ["a", "\ud800", "b"]}, dtype=object),
            categorical_columns=["c"],
            numeric_domains={},
            epsilon=2.0,
            delta=1e-6,
        )
        assert "c" in snap["columns"]

    @pytest.mark.parametrize(
        "value",
        [1 + 2j, np.complex64(1 + 2j), np.complex128(1 + 2j)],
        ids=["builtin", "complex64", "complex128"],
    )
    def test_every_complex_width_is_unconvertible_not_silently_real(self, value):
        """C-B2 was closed with `isinstance(raw, complex)`, which catches
        the builtin and `complex128` (it subclasses `complex`) but NOT
        `complex64`, where `float()` silently returns the real part. A
        parametrized test is the point here: the unparametrized version
        passed on the two widths that happen to subclass `complex` while
        the third kept `1.0`."""
        series = pd.Series([value], dtype=object)
        assert _normalize_numeric(series, lower=0.0, upper=10.0) == []

    def test_real_numeric_types_survive_the_complex_guard(self):
        """The guard drops complex only. Widening it to something that
        also rejects ordinary numpy reals would silently empty real
        columns, so pin the negative case alongside."""
        series = pd.Series([np.float64(3.0), np.int64(4), 5.0, "6"], dtype=object)
        assert _normalize_numeric(series, lower=0.0, upper=10.0) == [3.0, 4.0, 5.0, 6.0]

    def test_null_check_that_raises_on_content_does_not_take_the_fit_down(self):
        """C-B4, third location: deciding nullness runs the value's own
        dunders, so the null step is content-dependent too.
        `pd.isna(Decimal("sNaN"))` raises `InvalidOperation`, which the
        vectorized `Series.dropna()` propagated from outside the
        conversion guard. A row whose null check cannot be evaluated is
        not fatal -- which is the claim under test, and it is unchanged.

        Since round 7 the `Decimal` is dropped by the type gate rather
        than labelled, so it contributes no element. The C-B4 property
        this test exists for is TOTALITY, not presence: the fit must not
        raise on content. It does not."""
        series = pd.Series(["a", decimal.Decimal("sNaN"), "b"], dtype=object)
        assert _normalize_categorical(series) == ["a", "b"]

    def test_per_row_null_exclusion_excludes_every_null_flavour(self):
        """The null step went from vectorized to per-row, so pin what it
        must still do: every null flavour excluded.

        This no longer claims equivalence to `dropna()`. Round 7 narrowed
        the labelled domain to `str`/`bool`/reals, so array-valued cells
        drop at the type gate where `dropna()` kept them. That is a
        deliberate widening of the drop, not a regression -- dropping is
        recordwise and can only coarsen a release. The equivalence claim
        was itself the round-5 defect (it was asserted and false), so it
        is stated as the narrower true thing instead."""
        nulls = pd.Series(["a", None, np.nan, pd.NA, pd.NaT, "b"], dtype=object)
        assert _normalize_categorical(nulls) == ["a", "b"]
        arrays = pd.Series(["a", [1, 2], {"k": 1}, "b"], dtype=object)
        assert _normalize_categorical(arrays) == ["a", "b"]

    @pytest.mark.parametrize(
        "container",
        [[None], [np.nan], np.array([np.nan]), [1, 2], np.array([1, 2]), []],
        ids=["list_none", "list_nan", "array_nan", "list_two", "array_two", "empty"],
    )
    def test_container_cells_are_treated_uniformly_whatever_their_length(self, container):
        """Codex round 5: `pd.isna` on a container returns an ARRAY of
        per-element verdicts, not a verdict about the cell. The previous
        `bool(pd.isna(raw))` check raised for a MULTI-element array, so
        those cells took one path, but for a SINGLETON it silently
        returned that one element's verdict and took another. Container
        cells were being treated differently BY LENGTH, which is the
        regression this pins.

        Round 7 changed the disposition from "present and labelled" to
        "dropped at the type gate", but the invariant is the same one and
        is what matters: length must not decide. All six containers now
        drop uniformly.

        The earlier version of this test used only `[1, 2]`, whose
        two-element array takes the raising path, so it passed while the
        singleton regression was live. Parametrizing across lengths is
        the whole point."""
        series = pd.Series(["a", container, "b"], dtype=object)
        assert _normalize_categorical(series) == ["a", "b"]

    def test_normalize_categorical_keeps_ordinary_non_ascii_labels(self):
        """The encodability guard drops only what cannot be represented.
        Ordinary non-ASCII text takes the non-`isascii()` branch and must
        survive it, or the guard would silently gut international data."""
        series = pd.Series(["café", "日本語", "naïve"], dtype=object)
        assert _normalize_categorical(series) == ["café", "日本語", "naïve"]

    def test_fit_succeeds_identically_on_neighbours_differing_by_a_hostile_row(self):
        """C-B4, the channel itself: fit success is an observable. If one
        neighbour emits an artifact and the other raises, that observable
        has probability 0 on one and 1 on the other, breaking
        (epsilon, delta) for any delta < 1 before any released number is
        considered. Both neighbours must fit, and the released shape must
        not reveal which one it was."""
        base = pd.DataFrame({"n": ["1", "2"], "c": ["a", "b"]}, dtype=object)
        base_snap = fit_dp_snapshot(
            base,
            categorical_columns=["c"],
            numeric_domains={"n": (0.0, 10.0)},
            epsilon=2.0,
            delta=1e-6,
        )
        for hostile in _HOSTILE_SCALARS:
            neighbour = pd.DataFrame(
                {"n": ["1", "2", hostile], "c": ["a", "b", hostile]}, dtype=object
            )
            snap = fit_dp_snapshot(
                neighbour,
                categorical_columns=["c"],
                numeric_domains={"n": (0.0, 10.0)},
                epsilon=2.0,
                delta=1e-6,
            )
            assert snap["columns"].keys() == base_snap["columns"].keys()
            for name in base_snap["columns"]:
                assert (
                    snap["columns"][name]["stats"].keys()
                    == base_snap["columns"][name]["stats"].keys()
                ), (hostile, name)

    @pytest.mark.parametrize(
        ("arrow_type", "in_range", "out_of_range"),
        [
            (pa.timestamp("s"), 0, 2**60),
            (pa.timestamp("ms"), 0, 2**60),
            (pa.timestamp("us"), 0, 2**62),
            (pa.duration("s"), 0, 2**60),
            (pa.duration("ms"), 0, 2**60),
            (pa.date64(), 0, 2**60),
            (pa.time64("us"), 0, 2**60),
        ],
        ids=["ts_s", "ts_ms", "ts_us", "dur_s", "dur_ms", "date64", "time64_us"],
    )
    def test_a_pyarrow_backed_cell_that_cannot_be_fetched_does_not_fail_the_fit(
        self, arrow_type, in_range, out_of_range
    ):
        """dennis round 10 (BLOCKER-1): the per-cell FETCH is content-
        dependent, and it was outside every guard.

        `pd.ArrowDtype`'s `__iter__` calls `as_py()` on each element, which
        raises `OverflowError` whenever the stored integer leaves the
        corresponding Python range. The loop header is not somewhere a
        `try` can reach, so ONE such row took the whole fit down and made
        fit success a probability-0-versus-1 observable -- the same channel
        C-B4 names, reached through the third dtype backend rather than
        through a hostile scalar.

        Not adversarial-only: `pyarrow` is a hard dependency and
        `dtype_backend="pyarrow"` is a first-class option on `read_parquet`
        and `read_csv`, so an out-of-range timestamp is ordinary corrupt
        data in a column the caller correctly declared.

        `[ns]` is deliberately absent: int64 nanoseconds cannot leave the
        Python range, so it is the one SAFE arrow resolution. That is the
        exact inverse of round 9, where `ns` was the only unsafe numpy
        resolution -- a resolution axis established on one backend does not
        transfer to the other.

        Built through `pa.array` rather than `pd.Series(..., dtype=...)`
        because pandas' own scalar coercion refuses several of these
        types outright, while `read_parquet` hands back exactly this
        arrow-native array."""

        def arrow_series(values):
            return pd.Series(pd.arrays.ArrowExtensionArray(pa.array(values, type=arrow_type)))

        base = pd.DataFrame({"c": arrow_series([in_range, in_range]), "n": [1.0, 2.0]})
        neighbour = pd.DataFrame(
            {
                "c": arrow_series([in_range, in_range, out_of_range]),
                "n": [1.0, 2.0, 3.0],
            }
        )
        kwargs = {
            "categorical_columns": ["c"],
            "numeric_domains": {"n": (0.0, 10.0)},
            "epsilon": 2.0,
            "delta": 1e-6,
        }
        base_snap = fit_dp_snapshot(base, **kwargs)
        snap = fit_dp_snapshot(neighbour, **kwargs)
        assert snap["columns"].keys() == base_snap["columns"].keys()
        assert snap["columns"]["c"]["stats"].keys() == base_snap["columns"]["c"]["stats"].keys()

    def test_a_pyarrow_backed_cell_that_cannot_be_fetched_does_not_fail_a_numeric_fit(self):
        """The same channel on the numeric path, which has its own loop."""
        cells = pd.arrays.ArrowExtensionArray(pa.array([0, 1, 2**60], type=pa.timestamp("s")))
        neighbour = pd.DataFrame({"n": pd.Series(cells)})
        snap = fit_dp_snapshot(
            neighbour,
            categorical_columns=[],
            numeric_domains={"n": (0.0, 10.0)},
            epsilon=2.0,
            delta=1e-6,
        )
        assert "n" in snap["columns"]


class TestTotalityGuardStructure:
    """dennis round 11 (HIGH-2): three consecutive rounds of guard
    remediation landed in `dp_normalize` -- the `BaseException` widening,
    the `KeyboardInterrupt`/`SystemExit` re-raise, and the `GeneratorExit`
    split -- and not one of them had a regression test. Mutants reverting
    each of the three survived the whole suite. The only thing in the repo
    describing the guard structure was a docstring.

    CAREFUL when extending these: `_pytest.outcomes.Failed` derives from
    `BaseException`, not `Exception`, and is in no re-raise tuple, so an
    `assert` placed inside a mock cell's dunder is SWALLOWED by the guard
    and the test reports green (dennis round 11, MEDIUM-2). Every mock
    below records into a list the test inspects afterwards; none asserts
    inside a dunder.
    """

    def test_a_cell_raising_a_bare_base_exception_drops_rather_than_aborting(self):
        """Kills the mutant narrowing either guard back to `Exception`.

        Codex round 10's BLOCKER: `float()` on the numeric path has no type
        gate ahead of it, so a cell whose `__float__` raised a direct
        `BaseException` subclass escaped and made fit success a
        probability-0-versus-1 observable.

        Codex round 12 caught that the categorical half of this was inert:
        an object that merely defines a raising `__str__` never reaches
        `str()`, because `_canonical_label` rejects it at the type gate
        first, so `__str__` is never called and the categorical guard is
        never exercised. The value has to be a registered `numbers.Real`
        whose `__float__` raises, which reaches the `float()` inside
        `_canonical_label`, past the gate."""

        class Sentinel(BaseException):
            pass

        class NumericHostile:
            def __float__(self):
                raise Sentinel("from __float__ on the numeric path")

        @numbers.Real.register
        class CategoricalHostile:
            # Registered so it passes `_canonical_label`'s `numbers.Real`
            # gate and reaches its `float()`; that is the only categorical
            # route to a BaseException at the guard.
            def __float__(self):
                raise Sentinel("from __float__ inside _canonical_label")

        class ArrayHostile:
            # Reaches the categorical NULL-CHECK guard, which the label-raising
            # `CategoricalHostile` above does not: `pd.isna` calls a cell's
            # `__array__` (Codex round 13). With that guard narrowed to
            # `Exception`, this bare `BaseException` escapes and aborts the fit
            # instead of dropping the row.
            def __array__(self, *args, **kwargs):
                raise Sentinel("from __array__ in the null check")

        numeric = pd.Series([1.0, 2.0, NumericHostile()], dtype=object)
        assert _normalize_numeric(numeric, lower=0.0, upper=10.0) == [1.0, 2.0]
        categorical = pd.Series(["a", "b", CategoricalHostile()], dtype=object)
        assert _normalize_categorical(categorical) == ["a", "b"]
        null_check = pd.Series(["a", "b", ArrayHostile()], dtype=object)
        assert _normalize_categorical(null_check) == ["a", "b"]

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
    def test_an_interrupt_from_a_cell_still_propagates(self, interrupt):
        """Kills the mutant dropping `KeyboardInterrupt`/`SystemExit` from
        the two normalizer re-raise tuples. Totality is deliberately NOT
        absolute here: an operator must be able to stop a long fit and a
        caller must be able to exit the process. That residual is a
        documented domain restriction, so it needs a test as much as the
        guard does.

        Both paths, because they have separate guards (Codex round 12): the
        numeric one via `float()`, the categorical one via a registered
        `numbers.Real` whose `__float__` raises, since a plain object is
        rejected at the type gate before any dunder that could raise runs."""

        class NumericInterrupter:
            def __float__(self):
                raise interrupt

        @numbers.Real.register
        class CategoricalInterrupter:
            def __float__(self):
                raise interrupt

        with pytest.raises(interrupt):
            _normalize_numeric(
                pd.Series([1.0, NumericInterrupter()], dtype=object), lower=0.0, upper=1.0
            )
        with pytest.raises(interrupt):
            _normalize_categorical(pd.Series(["a", CategoricalInterrupter()], dtype=object))

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
    def test_an_interrupt_from_the_null_check_still_propagates(self, interrupt):
        """Kills the mutant narrowing the categorical NULL-CHECK guard to
        `Exception`, or dropping its `KeyboardInterrupt`/`SystemExit` re-raise
        (dennis + Codex round 13). Distinct from the label-guard case above:
        that one reaches `float()` inside `_canonical_label`; this one reaches
        the earlier `pd.isna(raw)`. An in-source note used to call this guard
        unreachable, on the theory that `pd.isna` never runs a cell's dunders.
        It does: `pd.isna` calls a cell's `__array__`, so a cell whose
        `__array__` raises an interrupt routes it straight through this guard,
        which must re-raise rather than swallow it into a dropped row. Without
        this test both null-guard mutants survived the whole suite."""

        class ArrayInterrupter:
            def __array__(self, *args, **kwargs):
                raise interrupt

        with pytest.raises(interrupt):
            _normalize_categorical(pd.Series(["a", ArrayInterrupter()], dtype=object))

    def test_len_that_raises_propagates_like_array(self):
        """`count = len(series)` shares the setup's fail-loud disposition
        with `series.array`: both are content-dependent container code, both
        propagate rather than silently degrade. The `array` test covers one
        half; this covers the length so a future guard around only one of
        them cannot reintroduce the silent-null release on the other."""

        class LenFails(pd.Series):
            boom = False

            def __len__(self):
                if type(self).boom:
                    raise SystemError("len depends on nothing this test controls")
                return super().__len__()

        series = LenFails([1.0, 2.0, 3.0])
        LenFails.boom = True
        try:
            with pytest.raises(SystemError):
                _normalize_numeric(series, lower=0.0, upper=10.0)
        finally:
            LenFails.boom = False

    def test_a_consumer_exception_thrown_into_cells_propagates(self):
        """Kills the mutant that wraps the `yield` in a guard which
        preserves finalization but swallows a consumer's `throw` (Codex
        round 12). The `yield` is outside every guard precisely so an
        exception injected by the consumer is the consumer's, not a fetch
        failure to be dropped."""
        generator = _cells(pd.Series([1.0, 2.0, 3.0], dtype=object))
        next(generator)
        with pytest.raises(ValueError):
            generator.throw(ValueError("injected by the consumer"))

    def test_a_container_cell_drops_under_every_boxing(self):
        """Codex round 12 (BLOCKER): an Arrow `list` column arrives cell by
        cell as a Python `list`, which `float()` rejects, so it drops. Add
        one null and pandas widens to `object`, reboxing each `list` as a
        length-1 `ndarray`, and `float(np.array([1]))` SUCCEEDS -- distance
        N on an N-row column against a `map(1)` certificate. Both boxings
        must drop, on both paths."""
        for arrow_type, value in (
            (pa.list_(pa.int64()), 1),
            (pa.large_list(pa.int64()), 2),
            (pa.list_(pa.float64()), 1.5),
        ):
            base = pd.Series(
                pd.arrays.ArrowExtensionArray(pa.array([[value], [value]], type=arrow_type))
            )
            neighbour = pd.concat([base, pd.Series([None])], ignore_index=True)
            assert base.array[0].__class__ is not neighbour.array[0].__class__  # boxing changed
            assert _normalize_numeric(base, lower=0.0, upper=1e9) == []
            assert _normalize_numeric(neighbour, lower=0.0, upper=1e9) == []
            assert _normalize_categorical(base) == []
            assert _normalize_categorical(neighbour) == []

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
    def test_an_interrupt_from_the_FETCH_also_propagates(self, interrupt):
        """The re-raise tuple in `_cells` is separate from the ones in the
        two normalizers, and the test above only covers the latter: it
        raises from `__float__`, which the numeric guard catches, never
        reaching the fetch. A mutant that emptied `_cells`'s tuple survived
        because of exactly that gap."""

        class RaisingGetItem:
            def __init__(self, values, interrupt):
                self._values, self._interrupt = list(values), interrupt

            def __len__(self):
                return len(self._values)

            def __getitem__(self, index):
                if self._values[index] == 999.0:
                    raise self._interrupt
                return self._values[index]

        class ArraySeries(pd.Series):
            _raising = None

            @property
            def array(self):
                return self._raising

        series = ArraySeries([1.0, 999.0, 2.0])
        series._raising = RaisingGetItem([1.0, 999.0, 2.0], interrupt)

        with pytest.raises(interrupt):
            list(_cells(series))
        with pytest.raises(interrupt):
            _normalize_numeric(series, lower=0.0, upper=10.0)

    def test_cells_stays_a_conforming_generator_when_finalized_early(self):
        """Kills the mutant moving the `yield` back inside the fetch guard.
        The `yield` is outside every guard, so a finalization `GeneratorExit`
        propagates from it; if a guard wrapped the `yield` and swallowed it,
        CPython would raise `RuntimeError: generator ignored GeneratorExit`
        and every consumer that stops early would see it."""
        series = pd.Series([1.0, 2.0, 3.0, 4.0], dtype=object)

        generator = _cells(series)
        next(generator)
        generator.close()  # raises RuntimeError if the generator is non-conforming

        for _ in itertools.islice(_cells(series), 2):
            pass

        for _ in _cells(series):
            break

        abandoned = _cells(series)
        next(abandoned)
        del abandoned
        gc.collect()

    def test_a_generator_exit_raised_by_the_fetch_drops_one_row(self):
        """Codex round 11: with the fetch and the `yield` in one `try`, a
        `GeneratorExit` arriving FROM the fetch was indistinguishable from
        finalization, and the re-raise added for the latter aborted the fit
        for the former -- so an array raising it from `__getitem__` took the
        whole fit down while the docstring promised one dropped row."""

        class RaisingGetItem:
            def __init__(self, values):
                self._values = list(values)

            def __len__(self):
                return len(self._values)

            def __getitem__(self, index):
                if self._values[index] == 999.0:
                    raise GeneratorExit("raised by __getitem__")
                return self._values[index]

        class ArraySeries(pd.Series):
            _raising = None

            @property
            def array(self):
                return self._raising

        series = ArraySeries([1.0, 999.0, 2.0])
        series._raising = RaisingGetItem([1.0, 999.0, 2.0])

        assert list(_cells(series)) == [1.0, 2.0]
        assert _normalize_numeric(series, lower=0.0, upper=10.0) == [1.0, 2.0]

    def test_content_dependent_setup_fails_loud_rather_than_releasing_nulls(self):
        """The disposition for a container whose setup runs content-dependent
        code, decided across three rounds.

        Codex round 11 wanted `series.array`/`len(series)` guarded so such a
        container did not abort the fit. Codex round 12 and dennis round 12
        both showed the guard I added was worse than the disease: swallowing
        the failure and returning [] emits an artifact asserting the column
        is ~100% null, which is both a lie about the data and an unbounded
        multiset difference from the frame's one-row neighbour dressed up as
        success. The input is out of domain either way -- a container whose
        setup executes caller code is as live as an object in a cell (see
        `dp_normalize`'s DOMAIN block) -- so the choice is between two
        out-of-domain dispositions, and failing LOUD is the honest one: it
        never emits a lying release, and it matches how a cell-dunder
        interrupt is handled. So the setup is unguarded and the exception
        propagates."""

        class SetupFails(pd.Series):
            @property
            def array(self):
                if len(self) > 2:
                    raise SystemError("setup depends on row count")
                return pd.Series.array.fget(self)

        with pytest.raises(SystemError):
            _normalize_numeric(SetupFails([1.0, 2.0, 3.0]), lower=0.0, upper=10.0)
        # A frame whose setup does NOT raise is read normally.
        assert _normalize_numeric(SetupFails([1.0, 2.0]), lower=0.0, upper=10.0) == [1.0, 2.0]


class TestReleaseIds:
    def test_independent_fits_mint_distinct_release_ids(self):
        df = pd.DataFrame({"age": [1.0, 2.0, 3.0]})
        snap_a = fit_dp_snapshot(
            df,
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=1.0,
            delta=1e-6,
        )
        snap_b = fit_dp_snapshot(
            df,
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=1.0,
            delta=1e-6,
        )
        assert snap_a["dp"]["release_id"] != snap_b["dp"]["release_id"]

    def test_copied_snapshot_preserves_release_id(self):
        import copy
        import json

        df = pd.DataFrame({"age": [1.0, 2.0, 3.0]})
        snap = fit_dp_snapshot(
            df,
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=1.0,
            delta=1e-6,
        )
        copy_a = copy.deepcopy(snap)
        copy_b = json.loads(json.dumps(snap))
        assert copy_a["dp"]["release_id"] == snap["dp"]["release_id"]
        assert copy_b["dp"]["release_id"] == snap["dp"]["release_id"]


class TestArtifactShape:
    def test_null_count_is_derived_from_the_released_row_count_not_the_true_one(self):
        """D-H2 (dennis round 4): `null_count` is the noised row count
        minus the released non-null count. Substituting `len(frame)` for
        the released row count survived all 56 tests in this file, and
        that mutant is the most direct exact-value leak this artifact
        shape allows: `null_count + non_null_count` would equal the true
        row count exactly, so an adversary reads n off a released
        artifact with zero error and two add-one-row neighbours shift it
        deterministically by 1.

        The backend forces a released row count far from `len(frame)`,
        so the two candidate derivations cannot coincide."""
        frame = _mixed_df(n=100)
        forced_row_count = len(frame) + 500

        class _ForcedRowCountBackend:
            def count_measurement(self, eps_q: float) -> _FakeMeasurement:
                return _FakeMeasurement(certificate=0.1, released=forced_row_count)

            def numeric_measurement(self, eps_q: float, interior_edges: tuple[float, ...]):
                return _FakeMeasurement(certificate=0.1, released=[1] * (len(interior_edges) + 1))

            def categorical_measurements(self, eps_q: float, delta_alloc: float):
                return (
                    _FakeMeasurement(certificate=(0.1, 1e-9), released={"CA": 7}),
                    _FakeMeasurement(certificate=0.1, released=9),
                )

        snap = _fit_dp_snapshot_with_backend(
            frame,
            categorical_columns=["state"],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=5.0,
            delta=1e-3,
            _session_backend=_ForcedRowCountBackend(),
        )
        assert snap["row_count"] == forced_row_count
        for name in ("age", "state"):
            column = snap["columns"][name]
            expected = max(0, forced_row_count - column["non_null_count"])
            assert column["null_count"] == expected, name
            # The true-row-count mutant would land here instead.
            assert column["null_count"] != max(0, len(frame) - column["non_null_count"]), name

    def test_dp_artifact_emits_no_exact_moments_or_quantiles(self):
        """D-M5: renamed from `..._no_exact_column_scalars`, which
        claimed more than it checked. It reads moments, quantiles, and
        two absent keys only; it says nothing about `null_count`,
        `non_null_count`, `distinct_count`, `other_count`, or
        `row_count`. The `null_count` half of that broader claim is
        carried by `test_null_count_is_derived_from_the_released_row_
        count_not_the_true_one` above, which is why the true-row-count
        mutant survived this one."""
        snap = fit_dp_snapshot(
            _mixed_df(),
            categorical_columns=["state"],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=2.0,
            delta=1e-6,
        )
        age = snap["columns"]["age"]
        assert age["stats"]["mean"] is None
        assert age["stats"]["std"] is None
        assert age["stats"]["quantiles"] == {}
        state = snap["columns"]["state"]
        assert "high_cardinality" not in state["stats"]
        assert "support_origin" not in state

    def test_dp_artifact_records_opendp_and_dp_accounting_versions(self):
        import importlib.metadata

        snap = fit_dp_snapshot(
            pd.DataFrame({"age": [1.0, 2.0]}),
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=1.0,
            delta=1e-6,
        )
        assert snap["dp"]["opendp_version"] == importlib.metadata.version("opendp")
        assert snap["dp"]["dp_accounting_version"] == importlib.metadata.version("dp-accounting")

    def test_dp_artifact_totals_are_the_accountant_result_not_the_request(self):
        snap = fit_dp_snapshot(
            _mixed_df(),
            categorical_columns=["state"],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=2.0,
            delta=1e-6,
        )
        assert snap["dp"]["epsilon_total"] <= 2.0
        assert snap["dp"]["epsilon_total"] != 2.0  # the accountant's result, not the request

    def test_dp_artifact_epsilon_total_matches_independent_dp_accounting_composition(self):
        """C-H3 (Codex): the test above asserts only `epsilon_total <=
        request` and `!= request`, so a defect writing e.g. `request / 2`
        (or any other formula that never consults `dp_accounting`) would
        pass without ever being compared against the library's own
        composition. This fixes EVERY certificate the fit's four queries
        would produce to known constants (independent of `eps_q`), then
        independently composes those SAME constants through `dp_budget.
        _compose`/`dp_accounting.pld` right here in the test, and asserts
        the artifact's `epsilon_total` equals that independently-derived
        value -- not merely an inequality against the request."""
        from decoy_engine.quality.dp_budget import _compose

        row_count_cert = 0.11
        numeric_cert = 0.17
        grouped_cert = (0.23, 1e-8)
        total_cert = 0.13

        class _FixedCertBackend:
            def count_measurement(self, eps_q: float) -> _FakeMeasurement:
                return _FakeMeasurement(certificate=row_count_cert, released=10)

            def numeric_measurement(self, eps_q: float, interior_edges: tuple[float, ...]):
                return _FakeMeasurement(
                    certificate=numeric_cert, released=[0] * (len(interior_edges) + 1)
                )

            def categorical_measurements(self, eps_q: float, delta_alloc: float):
                return (
                    _FakeMeasurement(certificate=grouped_cert, released={}),
                    _FakeMeasurement(certificate=total_cert, released=0),
                )

        snap = _fit_dp_snapshot_with_backend(
            _mixed_df(),
            categorical_columns=["state"],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=5.0,
            delta=1e-3,
            _session_backend=_FixedCertBackend(),
        )
        expected = _compose(
            [row_count_cert, numeric_cert, grouped_cert, total_cert]
        ).get_epsilon_for_delta(1e-3)
        assert snap["dp"]["epsilon_total"] == pytest.approx(expected)
        # A defect writing `request / 2` (2.5) or the raw request (5.0)
        # would not coincide with the composed result of these constants.
        assert snap["dp"]["epsilon_total"] not in (2.5, 5.0)

    def test_dp_fit_certificate_count_equals_query_count(self):
        snap = fit_dp_snapshot(
            _mixed_df(),
            categorical_columns=["state"],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=2.0,
            delta=1e-6,
        )
        assert snap["dp"]["query_count"] == 1 + 1 + 2  # row_count + 1 numeric + 2*1 categorical

    def test_certificate_count_mismatch_guard_raises_dp_schedule_mismatch(self, monkeypatch):
        """C-H3 (Codex): the test above only checks the serialized formula
        `1 + numeric_count + 2 * categorical_count` against `query_count`;
        removing the runtime `session.certificate_count() != schedule.
        query_count` guard at the call site in `dp.py` does not affect it,
        since the loop above always releases every declared column exactly
        once by construction. This forces the guard's precondition
        directly (patching `certificate_count` to disagree with the
        schedule) and asserts the typed error the guard raises -- deleting
        the guard would make this pass cleanly instead of raising."""
        from decoy_engine.quality import dp_budget

        monkeypatch.setattr(dp_budget.OpenDpReleaseSession, "certificate_count", lambda self: 999)
        with pytest.raises(DpBudgetError) as exc:
            fit_dp_snapshot(
                _mixed_df(),
                categorical_columns=["state"],
                numeric_domains={"age": (0.0, 120.0)},
                epsilon=2.0,
                delta=1e-6,
            )
        assert exc.value.code == "dp_schedule_mismatch"

    def test_adapter_never_rounds_or_compares_before_release(self):
        """C-H3 row 1c (guide section 6): OpenDP owns exact noisy-value
        thresholding; Decoy may round (serialize) only AFTER release, and
        must never compare a value against a threshold of its own before
        that. This records every value handed to a measurement's
        `.invoke()` at the seam and asserts each equals the UNROUNDED,
        un-thresholded normalized projection -- proving `dp.py` passes the
        already-normalized values straight through with no rounding or
        comparison of its own -- and separately asserts the module exposes
        no home-grown threshold helper or module-level threshold
        arithmetic."""
        recorded: dict[str, list[object]] = {}

        class _RecordingBackend:
            def count_measurement(self, eps_q: float) -> _FakeMeasurement:
                def released_fn(values):
                    recorded["row_count"] = list(values)
                    return len(values)

                return _FakeMeasurement(certificate=0.1, released_fn=released_fn)

            def numeric_measurement(self, eps_q: float, interior_edges: tuple[float, ...]):
                def released_fn(values):
                    recorded["numeric"] = list(values)
                    return [0] * (len(interior_edges) + 1)

                return _FakeMeasurement(certificate=0.1, released_fn=released_fn)

            def categorical_measurements(self, eps_q: float, delta_alloc: float):
                def grouped_fn(values):
                    recorded["grouped"] = list(values)
                    return {}

                def total_fn(values):
                    recorded["total"] = list(values)
                    return len(values)

                return (
                    _FakeMeasurement(certificate=(0.1, 1e-8), released_fn=grouped_fn),
                    _FakeMeasurement(certificate=0.1, released_fn=total_fn),
                )

        df = pd.DataFrame(
            {
                "age": [1.5, 200.0, float("nan"), None, 50.25],
                "cat": ["a", "b", None, "a", "c"],
            }
        )
        _fit_dp_snapshot_with_backend(
            df,
            categorical_columns=["cat"],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=2.0,
            delta=1e-6,
            _session_backend=_RecordingBackend(),
        )
        expected_numeric = _normalize_numeric(df["age"], lower=0.0, upper=120.0)
        expected_cat = _normalize_categorical(df["cat"])
        assert recorded["numeric"] == expected_numeric
        # Unrounded floats reach the measurement -- 1.5/50.25 stay
        # fractional, 200.0 is clamped to the domain bound (120.0) but not
        # rounded to an int.
        assert all(isinstance(v, float) for v in recorded["numeric"])
        assert 120.0 in recorded["numeric"] and 1.5 in recorded["numeric"]
        assert recorded["grouped"] == expected_cat
        assert recorded["total"] == expected_cat

        import decoy_engine.quality.dp as dp_module

        assert not hasattr(dp_module, "_stable_histogram_threshold")
        # D-L5: `"tau" not in vars(...)` was an exact-key check, so
        # `_TAU`, `threshold_tau`, or any differently-named threshold
        # helper passed it. Match on the name shape instead: OpenDP owns
        # thresholding, so this module should carry no tau-ish symbol at
        # all, whatever it is called.
        tau_named = [name for name in vars(dp_module) if "tau" in name.lower()]
        assert tau_named == [], tau_named


class TestDisclosureChannelRegressions:
    """Guide section 7.3: private values change, public declarations stay
    fixed; only the observable schema/success class/schedule may match,
    never the released values themselves."""

    def test_dp_fit_kind_and_success_are_identical_across_30_31_distinct_neighbors(self):
        """C-H3 (Codex): the previous version compared only `kind` and
        `query_count`, so a cardinality branch that changed the emitted
        KEY SET (e.g. adding/omitting a stats key past some distinct-value
        threshold) would pass unnoticed. This additionally compares the
        full column-entry key set and the full `stats` key set between
        the two neighbours, plus the fit's own declared-column echo
        (`dp.categorical_columns`) -- any cardinality-triggered branch
        that changes what keys the artifact carries now fails here."""
        df_30 = pd.DataFrame({"cat": [f"val{i}" for i in range(30)]})
        df_31 = pd.DataFrame({"cat": [f"val{i}" for i in range(31)]})
        snap_30 = fit_dp_snapshot(
            df_30, categorical_columns=["cat"], numeric_domains={}, epsilon=2.0, delta=1e-6
        )
        snap_31 = fit_dp_snapshot(
            df_31, categorical_columns=["cat"], numeric_domains={}, epsilon=2.0, delta=1e-6
        )
        col_30, col_31 = snap_30["columns"]["cat"], snap_31["columns"]["cat"]
        assert col_30["kind"] == col_31["kind"] == "categorical"
        assert snap_30["dp"]["query_count"] == snap_31["dp"]["query_count"]
        assert snap_30["dp"]["categorical_columns"] == snap_31["dp"]["categorical_columns"]
        # C-H-1 (Codex round 6): key-set comparison cannot catch a leaked
        # cardinality PREDICATE, because a key like
        # `over_30_distinct = len(set(values)) > 30` is present on both
        # neighbours and only its VALUE differs. These two fixtures are
        # the only ones in the suite that straddle such a threshold, so
        # they are where it has to be caught. Same property as the
        # all-null and all-inf tests: every field is either a released
        # noised quantity or a public constant.
        assert _public_part(col_30) == _public_part(col_31)
        assert snap_30["dp"].keys() == snap_31["dp"].keys()

    def test_dp_fit_all_null_declared_categorical_runs_measurement_and_emits_categorical_shape(
        self,
    ):
        """C-H3 (Codex), F1: the previous version recorded no mechanism
        invocation and made no nonempty comparison, so an all-null
        short-circuit that skipped the measurement entirely but returned
        the expected shape would still pass. This uses a spy backend
        (`released_fn`) to prove BOTH scheduled categorical measurements
        are actually invoked on the all-null input, then asserts the
        released values are consulted (not merely a hard-coded shape)."""
        invoked: list[str] = []

        class _SpyAllNullBackend:
            def count_measurement(self, eps_q: float) -> _FakeMeasurement:
                def released_fn(values):
                    invoked.append("row_count")
                    return len(values)

                return _FakeMeasurement(certificate=0.1, released_fn=released_fn)

            def numeric_measurement(self, eps_q: float, interior_edges: tuple[float, ...]):
                raise AssertionError("no numeric column declared; must not be called")

            def categorical_measurements(self, eps_q: float, delta_alloc: float):
                def grouped_fn(values):
                    invoked.append("grouped")
                    return {}

                def total_fn(values):
                    invoked.append("total")
                    return len(values)

                return (
                    _FakeMeasurement(certificate=(0.1, 1e-8), released_fn=grouped_fn),
                    _FakeMeasurement(certificate=0.1, released_fn=total_fn),
                )

        df = pd.DataFrame({"cat": [None, None, None, None]})
        snap = _fit_dp_snapshot_with_backend(
            df,
            categorical_columns=["cat"],
            numeric_domains={},
            epsilon=2.0,
            delta=1e-6,
            _session_backend=_SpyAllNullBackend(),
        )
        # Both categorical measurements were actually invoked (never
        # short-circuited), each exactly once, on a values list that IS
        # length-zero (all four rows were null, correctly excluded) --
        # not skipped, just correctly empty.
        assert invoked.count("grouped") == 1
        assert invoked.count("total") == 1
        assert snap["columns"]["cat"]["kind"] == "categorical"
        assert snap["dp"]["query_count"] == 1 + 2  # row_count + 2 categorical queries

        # Codex round 5: this test named selected fields but never
        # compared its artifact against a NON-null fit, so a production
        # branch exposing a private predicate (`all_null = not values`)
        # would pass. Comparing key SETS does not catch that either: such
        # a key is added unconditionally, so both neighbours carry it and
        # only the VALUE differs. The property that actually holds is
        # that every field is either a released quantity or a public
        # constant, so strip the released ones and require the rest to
        # be identical.
        ordinary = _fit_dp_snapshot_with_backend(
            pd.DataFrame({"cat": ["a", "b", "a", "c"]}),
            categorical_columns=["cat"],
            numeric_domains={},
            epsilon=2.0,
            delta=1e-6,
            _session_backend=_SpyAllNullBackend(),
        )
        assert _public_part(snap["columns"]["cat"]) == _public_part(ordinary["columns"]["cat"])
        assert snap.keys() == ordinary.keys()
        assert snap["dp"].keys() == ordinary["dp"].keys()

    def test_dp_fit_numeric_shape_and_charge_schedule_are_data_independent_for_all_null_and_all_inf(
        self,
    ):
        domains = {"age": (0.0, 120.0)}
        all_null = fit_dp_snapshot(
            pd.DataFrame({"age": [None, None, None]}),
            categorical_columns=[],
            numeric_domains=domains,
            epsilon=2.0,
            delta=1e-6,
        )
        all_inf = fit_dp_snapshot(
            pd.DataFrame({"age": [float("inf"), float("-inf"), float("inf")]}),
            categorical_columns=[],
            numeric_domains=domains,
            epsilon=2.0,
            delta=1e-6,
        )
        ordinary = fit_dp_snapshot(
            pd.DataFrame({"age": [10.0, 50.0, 90.0]}),
            categorical_columns=[],
            numeric_domains=domains,
            epsilon=2.0,
            delta=1e-6,
        )
        for snap in (all_null, all_inf, ordinary):
            assert (
                snap["columns"]["age"]["stats"]["bin_edges"]
                == ordinary["columns"]["age"]["stats"]["bin_edges"]
            )
            assert len(snap["columns"]["age"]["stats"]["bin_counts"]) == 10
            assert snap["dp"]["query_count"] == ordinary["dp"]["query_count"]
            # C-B2 (Codex round-3 blocker): the previous version of this
            # test compared only bin edges, bin-count length, and query
            # count, and missed a differing `dtype` -- the parked
            # `canonical_dtype_label(frame[col].dtype)` emitted "float64"
            # for the all-null/all-inf frames (already float dtype) but
            # would emit "int64"/"object" for other private inputs under
            # the identical public declaration. dtype is now a function of
            # declared kind alone, so it must be identical across every
            # neighbour here regardless of the frame's own pandas dtype.
            assert snap["columns"]["age"]["dtype"] == ordinary["columns"]["age"]["dtype"]
            # Codex round 5: naming individual fields cannot catch a
            # leaked predicate such as `all_null = not values`, and nor
            # can comparing key sets, since that key is present on both
            # neighbours and only its VALUE differs. Strip the released
            # quantities and require everything else to be identical.
            assert _public_part(snap["columns"]["age"]) == _public_part(ordinary["columns"]["age"])
            assert snap.keys() == ordinary.keys()
            assert snap["dp"].keys() == ordinary["dp"].keys()

    def test_dp_numeric_dtype_is_identical_across_int64_and_float64_input_frames(self):
        """C-B2 (Codex round-3 blocker), direct reproduction: `[1]` (a
        pandas int64 column) versus `[1, None]` (float64 -- pandas
        upcasts an integer column the moment a null enters it) under
        IDENTICAL public declarations used to emit `dtype: "int64"` and
        `dtype: "float64"` respectively -- a private-value-dependent
        disclosure with no caller declaration behind it. Both must now
        emit the same dtype label, derived only from declared kind."""
        int_only = fit_dp_snapshot(
            pd.DataFrame({"age": [1]}),
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=2.0,
            delta=1e-6,
        )
        with_null = fit_dp_snapshot(
            pd.DataFrame({"age": [1, None]}),
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=2.0,
            delta=1e-6,
        )
        assert int_only["columns"]["age"]["dtype"] == with_null["columns"]["age"]["dtype"]

    def test_dp_fit_emits_no_warning_regardless_of_a_complex_valued_neighbor(self):
        """C-B2 (Codex round-3 blocker), direct reproduction: two numeric
        neighbours with identical public declarations and pandas dtype
        `object`, `["1"]` versus `["1", 1+2j]` (adding one row containing
        a complex value). The parked vectorized `pd.to_numeric(...).
        to_numpy(dtype=float)` path deterministically emitted
        `ComplexWarning` for the second neighbour and nothing for the
        first -- an observable with probability 0 on one neighbour and 1
        on the other, which alone violates any (epsilon, delta) guarantee
        with delta < 1. Both neighbours must now emit zero warnings."""
        import warnings

        domains = {"age": (0.0, 10.0)}
        neighbor_a = pd.DataFrame({"age": pd.Series(["1"], dtype=object)})
        neighbor_b = pd.DataFrame({"age": pd.Series(["1", 1 + 2j], dtype=object)})
        for neighbor in (neighbor_a, neighbor_b):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                fit_dp_snapshot(
                    neighbor,
                    categorical_columns=[],
                    numeric_domains=domains,
                    epsilon=2.0,
                    delta=1e-6,
                )
            assert caught == [], [str(w.message) for w in caught]

    def test_all_distinct_categorical_versus_single_repeated_label_same_schema(self):
        all_distinct = pd.DataFrame({"cat": [f"v{i}" for i in range(50)]})
        one_label = pd.DataFrame({"cat": ["only"] * 50})
        snap_distinct = fit_dp_snapshot(
            all_distinct, categorical_columns=["cat"], numeric_domains={}, epsilon=2.0, delta=1e-6
        )
        snap_one = fit_dp_snapshot(
            one_label, categorical_columns=["cat"], numeric_domains={}, epsilon=2.0, delta=1e-6
        )
        assert snap_distinct["dp"]["query_count"] == snap_one["dp"]["query_count"]
        # Neither raises, and other_count is well-formed (no divide-by-zero
        # or exact fallback) even when nothing is retained.
        assert snap_distinct["columns"]["cat"]["stats"]["other_count"] >= 0
        assert snap_one["columns"]["cat"]["stats"]["other_count"] >= 0


class _FixedCategoricalBackend:
    """H3/H4 seam (guide section 5 step 3 / section 6 rows 1a/1b): forces
    `fit_dp_snapshot`'s categorical release to a FIXED grouped-count dict
    and non-null total regardless of `values`, so a test can vary the
    real, suppressed input while observing an artifact whose released
    categorical quantities are pinned. `count_measurement`/`numeric_
    measurement` are also fixed so the WHOLE artifact (not just the
    categorical column) is reproducible across different input frames,
    letting a byte-identity assertion cover more than the one column."""

    def __init__(self, *, row_count: int, grouped: dict[str, int], total: int) -> None:
        self._row_count = row_count
        self._grouped = dict(grouped)
        self._total = total

    def count_measurement(self, eps_q: float) -> _FakeMeasurement:
        return _FakeMeasurement(certificate=0.01, released=self._row_count)

    def numeric_measurement(self, eps_q: float, interior_edges: tuple[float, ...]):
        return _FakeMeasurement(certificate=0.01, released=[0] * (len(interior_edges) + 1))

    def categorical_measurements(self, eps_q: float, delta_alloc: float):
        return (
            _FakeMeasurement(certificate=(0.01, delta_alloc / 2 or 1e-9), released=self._grouped),
            _FakeMeasurement(certificate=0.01, released=self._total),
        )


class TestCategoricalRelease:
    def test_categorical_release_order_uses_noised_counts_not_true_rank(self):
        """Defect 1a (guide section 6): the previous version of this test
        used a fixture whose true counts (~1500/900/450/150 at epsilon
        5.0) are so far apart that Laplace noise cannot plausibly invert
        them -- the assertion held whether the code sorted by released
        count (correct) or by true rank (the defect), so it never
        falsified anything. This version uses the `_session_backend` seam
        to force a released grouped-count dict that INVERTS the true
        frequency order of its input frame, then asserts the artifact's
        `top_values` follows the FORCED (inverted) order. Sorting by true
        rank instead of released count would emit CA/NY/TX; sorting by
        released count (correct) emits TX/NY/CA."""
        # True frequency order in the input: CA (900) > NY (90) > TX (10).
        df = pd.DataFrame({"state": ["CA"] * 900 + ["NY"] * 90 + ["TX"] * 10})
        # Forced RELEASED order inverts it: TX highest, NY middle, CA lowest.
        backend = _FixedCategoricalBackend(
            row_count=1000, grouped={"CA": 10, "NY": 90, "TX": 900}, total=1000
        )
        snap = _fit_dp_snapshot_with_backend(
            df,
            categorical_columns=["state"],
            numeric_domains={},
            epsilon=5.0,
            delta=1e-6,
            _session_backend=backend,
        )
        top = snap["columns"]["state"]["stats"]["top_values"]
        assert [t["value"] for t in top] == ["TX", "NY", "CA"]
        assert [t["count"] for t in top] == [900, 90, 10]
        counts = [t["count"] for t in top]
        assert counts == sorted(counts, reverse=True)

    def test_categorical_other_count_is_derived_only_from_noised_total_and_kept_counts(self):
        """Defect 1b (guide section 6): the previous version of this test
        ran ONE scenario and restated `other_count`'s own formula line for
        line -- a tautology, not a proof of independence from suppressed
        values. This version runs TWO scenarios whose forced released
        total and kept pairs are IDENTICAL but whose true, suppressed
        (non-top) labels differ, and asserts the resulting artifacts are
        byte-identical except `release_id` (which always mints fresh per
        fit by design -- guide section 7.3/binding decision 10). Any
        dependence on the suppressed values -- for `other_count` or
        anything else in the artifact -- would make the two differ.

        BLOCKER B-2: an earlier version of this test made the two
        scenarios' suppressed tails differ ONLY in label NAMES (20 rows
        across 20 distinct labels in both, `rareA{i}` vs `rareB{i}`) --
        same suppressed MASS, same suppressed CARDINALITY. That falsifies
        dependence on label strings and nothing else: a defect reading the
        true suppressed row count or the true suppressed label count
        straight off the input (instead of deriving `other_count` from the
        noised total and kept counts alone) would compute the identical
        number for both scenarios and this test would not notice. The two
        tails below differ in both mass (20 rows vs. 40) and cardinality
        (20 distinct labels vs. 7), so a defect reading either true
        quantity off the suppressed values makes `snap_a` and `snap_b`
        disagree.

        D-H1 (dennis HIGH): that same earlier version ALSO gave both
        scenarios identical TRUE KEPT counts for the retained labels
        themselves (CA 300 / NY 150 / TX 50 in both, true kept sum 500 in
        both), even though the backend's FORCED released grouped dict and
        total are what `other_count` is supposed to depend on. A defect
        computing `other_count` from each retained label's TRUE count
        read off the raw input --

            other_count = max(0, non_null_total - sum(
                list(cat_values).count(label) for label, _count in retained
            ))

        -- reads `retained`'s LABELS from the forced (identical) released
        dict but each label's COUNT from the private input, so with
        identical true kept sums (500 in both) it still computes the
        identical `other_count` (20) for both scenarios and this test
        would not notice. df_b now gives the retained labels DIFFERENT
        true counts (CA 400 / NY 100 / TX 10, true kept sum 510) while the
        backend still forces the SAME released grouped dict/total for
        both -- the mutant above then computes 20 for `snap_a` and 10 for
        `snap_b` and the byte-identity assertion below catches it."""
        backend = _FixedCategoricalBackend(
            row_count=520, grouped={"CA": 300, "NY": 150, "TX": 50}, total=520
        )
        # Same kept LABELS and same released total (both forced by the
        # backend, identical for df_a and df_b); DIFFERENT true kept
        # COUNTS (500 vs 510) AND different suppressed tail mass/
        # cardinality (20 rows/20 labels vs. 40 rows/7 labels) -- both
        # kinds of "read the true value instead of the released one"
        # defect have a distinct private quantity to disagree on.
        df_a = pd.DataFrame(
            {"state": ["CA"] * 300 + ["NY"] * 150 + ["TX"] * 50 + [f"rareA{i}" for i in range(20)]}
        )
        rare_labels_b = [f"rareB{i}" for i in range(7)]
        tail_b = [rare_labels_b[i % len(rare_labels_b)] for i in range(40)]
        df_b = pd.DataFrame({"state": ["CA"] * 400 + ["NY"] * 100 + ["TX"] * 10 + tail_b})
        snap_a = _fit_dp_snapshot_with_backend(
            df_a,
            categorical_columns=["state"],
            numeric_domains={},
            epsilon=5.0,
            delta=1e-6,
            _session_backend=backend,
        )
        snap_b = _fit_dp_snapshot_with_backend(
            df_b,
            categorical_columns=["state"],
            numeric_domains={},
            epsilon=5.0,
            delta=1e-6,
            _session_backend=backend,
        )
        stats_a = snap_a["columns"]["state"]["stats"]
        kept_sum = sum(t["count"] for t in stats_a["top_values"])
        assert stats_a["other_count"] == max(
            0, snap_a["columns"]["state"]["non_null_count"] - kept_sum
        )

        del snap_a["dp"]["release_id"]
        del snap_b["dp"]["release_id"]
        assert snap_a == snap_b


class TestUnseededStatisticalMechanism:
    """Guide section 7.2. Neither test may be seeded; both are designed
    so a correct implementation fails less often than `_ALPHA` per run."""

    def test_independent_dp_fits_are_not_deterministic(self):
        df = pd.DataFrame({"age": [float(i % 120) for i in range(2000)]})
        domains = {"age": (0.0, 120.0)}
        vectors = [
            tuple(
                fit_dp_snapshot(
                    df, categorical_columns=[], numeric_domains=domains, epsilon=5.0, delta=1e-6
                )["columns"]["age"]["stats"]["bin_counts"]
            )
            for _ in range(5)
        ]
        # Collision probability of 5 independent 10-bin noised-count
        # vectors all matching, under a real Laplace mechanism at this
        # scale, is far below _ALPHA; a broken (e.g. zero-noise) mechanism
        # would make every vector identical.
        assert len(set(vectors)) > 1

    def test_count_one_release_probability_upper_bound(self):
        """A label with true count 1 must be released no more often than
        its measurement's own certified delta (times a documented slack),
        one-sided Clopper-Pearson at alpha=_ALPHA. Compares against the
        CERTIFIED delta of the thresholded chain, not the fit-wide delta
        or the allocation target (guide section 7.2).

        Calibration (the allocation search) runs ONCE: it is a pure
        function of the public (epsilon, delta, schedule) request, never
        of values (guide section 4.3.2), so re-deriving it per trial would
        only slow the test down, not change what it measures. The
        calibrated measurement is invoked fresh (unseeded OpenDP
        randomness) on every trial.

        The schedule's (epsilon, delta) here is a private test fixture,
        not a production default: it exists only to fix a certified delta
        against which the empirical release rate is checked, and the
        property under test (empirical rate tracks certified delta) holds
        at any delta. A one-categorical-column schedule at delta=1e-3
        (certified delta ~5e-4) needs an infeasible trial count to resolve
        at alpha=1e-6: with 1 expected release in 2000 trials the
        Clopper-Pearson upper bound is dominated by sampling noise (~16x
        certified delta), not by mechanism behavior, so the test cannot
        tell a correct implementation from a broken one. delta=0.02 raises
        the certified delta to ~0.01, so 5000 trials give ~50 expected
        releases; per Clopper-Pearson, that pins the upper bound to
        ~1.8-2.7x certified delta even in a 5-sigma-high draw (verified
        against scipy.stats.beta.ppf during test design), comfortably
        inside the slack factor below while still catching an
        order-of-magnitude regression in release probability.
        """
        from decoy_engine.quality.dp_budget import OpenDpReleaseSession
        from decoy_engine.quality.dp_schedule import CategoricalQuerySpec, Schedule

        schedule = Schedule(
            row_count_name="rc",
            numeric=(),
            categorical=(CategoricalQuerySpec("g", "t"),),
        )
        session = OpenDpReleaseSession(schedule, epsilon=1.0, delta=0.02)
        grouped_meas, _total_meas = session._categorical_measurements(
            session._eps_q, session._delta_per_categorical
        )
        certified_delta = grouped_meas.map(1)[1]

        n_trials = 5000
        values = ["common"] * 999 + ["rare_one"]
        releases = sum(1 for _ in range(n_trials) if "rare_one" in grouped_meas.invoke(values))

        # One-sided Clopper-Pearson upper bound at alpha=_ALPHA, computed
        # via the regularized incomplete beta function's relationship to
        # the F distribution (Clopper & Pearson 1934); stdlib-only so this
        # test carries no extra runtime dependency.
        upper = _clopper_pearson_upper(releases, n_trials, _ALPHA)
        slack_factor = 10  # documented slack: catches an order-of-magnitude regression
        assert upper <= certified_delta * slack_factor, (
            f"N={n_trials} releases={releases} observed_rate={releases / n_trials} "
            f"upper={upper} certified_delta={certified_delta}"
        )


def _clopper_pearson_upper(successes: int, n: int, alpha: float) -> float:
    """One-sided Clopper-Pearson upper confidence bound on a binomial
    success probability (Clopper & Pearson, "The Use of Confidence or
    Fiducial Limits Illustrated in the Case of the Binomial", Biometrika
    1934). `successes == n` gives the trivial upper bound of 1.0; otherwise
    the bound is the value `p` solving `Beta_CDF(observed_rate; successes+1,
    n-successes) = 1 - alpha`, found here by bisection over the
    regularized incomplete beta function (`math.lgamma`-based series, no
    scipy dependency) rather than scipy's `beta.ppf`, since scipy is not a
    declared runtime dependency of this engine."""
    if successes >= n:
        return 1.0

    def reg_incomplete_beta(x: float, a: float, b: float) -> float:
        # Continued-fraction evaluation (Numerical Recipes 6.4), accurate
        # to double precision for the parameter ranges this test uses.
        if x <= 0.0:
            return 0.0
        if x >= 1.0:
            return 1.0
        ln_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
        front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - ln_beta) / a
        if x >= (a + 1.0) / (a + b + 2.0):
            return 1.0 - reg_incomplete_beta(1.0 - x, b, a)
        f, c, d = 1.0, 1.0, 0.0
        for i in range(200):
            m = i // 2
            if i == 0:
                numerator = 1.0
            elif i % 2 == 0:
                numerator = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
            else:
                numerator = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
            d = 1.0 + numerator * d
            if abs(d) < 1e-30:
                d = 1e-30
            d = 1.0 / d
            c = 1.0 + numerator / c
            if abs(c) < 1e-30:
                c = 1e-30
            f *= d * c
            if abs(d * c - 1.0) < 1e-12:
                break
        return front * (f - 1.0)

    a, b = successes + 1, n - successes
    lo, hi = successes / n, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if reg_incomplete_beta(mid, a, b) < 1 - alpha:
            lo = mid
        else:
            hi = mid
    return hi
