"""Unit tests for `decoy_engine.quality.dp.fit_dp_snapshot` (DPS Scope B).

Supersedes the Option A `apply_dp_noise` suite entirely (that mechanism
is deleted). Covers the guide's named assertions for step 5 section 3
(config validation, release ID minting, disclosure-channel regressions,
categorical order/other_count derivation) plus the section 7.2 unseeded
statistical mechanism tests.
"""

from __future__ import annotations

import decimal
import math
import warnings

import numpy as np
import pandas as pd
import pytest

from decoy_engine.quality.dp import (
    DpError,
    _fit_dp_snapshot_with_backend,
    _normalize_categorical,
    _normalize_numeric,
    fit_dp_snapshot,
)
from decoy_engine.quality.dp_budget import DpBudgetError, _FakeMeasurement

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

        sig = inspect.signature(fit_dp_snapshot)
        assert set(sig.parameters) == {
            "frame",
            "categorical_columns",
            "numeric_domains",
            "epsilon",
            "delta",
            "numeric_bins",
        }

    def test_dp_fit_rejects_missing_public_column_declarations(self):
        df = pd.DataFrame({"age": [1.0, 2.0], "state": ["a", "b"]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df,
                categorical_columns=["state"],
                numeric_domains={},  # "age" undeclared
                epsilon=1.0,
                delta=1e-6,
            )
        assert exc.value.code == "dp_column_declaration_incomplete"

    def test_dp_fit_rejects_overlapping_public_column_declarations(self):
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df,
                categorical_columns=["age"],
                numeric_domains={"age": (0.0, 120.0)},
                epsilon=1.0,
                delta=1e-6,
            )
        assert exc.value.code == "dp_column_declaration_overlap"

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
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df, categorical_columns=[], numeric_domains={"age": bounds}, epsilon=1.0, delta=1e-6
            )
        assert exc.value.code == "dp_numeric_domain_invalid"

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

    def test_non_string_frame_column_labels_are_rejected(self):
        """D-L-A (dennis round 5): validation compared `str(label)` sets
        while the fit loop indexed with the stringified name, so an
        integer column `5` declared as `numeric_domains={5: ...}` passed
        validation and then died on `frame["5"]` with a bare `KeyError`.
        Fail-closed and no leak, but the wrong exception type from a
        module that documents coded errors."""
        df = pd.DataFrame({5: [0.5, 0.6]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df, categorical_columns=[], numeric_domains={5: (0.0, 1.0)}, epsilon=2.0, delta=1e-6
            )
        assert exc.value.code == "dp_column_label_not_a_string"

    def test_duplicate_frame_column_labels_are_rejected(self):
        """D-M4: `frame.columns` was compared as a set, so `["x", "x"]`
        passed the coverage check. `frame["x"]` then returns a DataFrame,
        which both normalizers iterate as column LABELS, and the fit was
        accepted while releasing a distribution over the string "x"
        rather than over the data. No leak, but a DP artifact that
        silently describes the wrong thing."""
        df = pd.DataFrame([["CA", "NY"], ["TX", "CA"]], columns=["x", "x"])
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df, categorical_columns=["x"], numeric_domains={}, epsilon=2.0, delta=1e-6
            )
        assert exc.value.code == "dp_column_declaration_duplicated"

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
            assert len(base_out) - len(dropped_out) in (0, 1)

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
            assert len(base_out) - len(dropped_out) in (0, 1)

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
        present, not fatal."""
        series = pd.Series(["a", decimal.Decimal("sNaN"), "b"], dtype=object)
        assert _normalize_categorical(series) == ["a", "sNaN", "b"]

    def test_per_row_null_exclusion_matches_dropna_semantics(self):
        """The null step went from vectorized to per-row, so pin what it
        must still do: every null flavour excluded, and array-valued
        cells (where `pd.isna` returns an array rather than a verdict)
        still present and labelled, as `dropna()` left them."""
        nulls = pd.Series(["a", None, np.nan, pd.NA, pd.NaT, "b"], dtype=object)
        assert _normalize_categorical(nulls) == ["a", "b"]
        arrays = pd.Series(["a", [1, 2], {"k": 1}, "b"], dtype=object)
        assert _normalize_categorical(arrays) == ["a", "[1, 2]", "{'k': 1}", "b"]

    @pytest.mark.parametrize(
        "container",
        [[None], [np.nan], np.array([np.nan]), [1, 2], np.array([1, 2]), []],
        ids=["list_none", "list_nan", "array_nan", "list_two", "array_two", "empty"],
    )
    def test_container_cells_are_present_whatever_their_length(self, container):
        """Codex round 5: `pd.isna` on a container returns an ARRAY of
        per-element verdicts, not a verdict about the cell. The previous
        `bool(pd.isna(raw))` check raised for a MULTI-element array, so
        those cells stayed present by accident, but for a SINGLETON it
        silently returned that one element's verdict and dropped
        `[None]` and `numpy.array([numpy.nan])`, which `dropna()` keeps.

        The earlier version of this test used only `[1, 2]`, whose
        two-element array takes the raising path, so it passed while the
        singleton regression was live. Parametrizing across lengths is
        the whole point."""
        series = pd.Series(["a", container, "b"], dtype=object)
        assert _normalize_categorical(series) == ["a", str(container), "b"]

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
        assert set(col_30.keys()) == set(col_31.keys())
        assert set(col_30["stats"].keys()) == set(col_31["stats"].keys())
        assert snap_30["dp"]["query_count"] == snap_31["dp"]["query_count"]
        assert snap_30["dp"]["categorical_columns"] == snap_31["dp"]["categorical_columns"]

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
