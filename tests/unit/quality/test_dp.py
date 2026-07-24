"""Unit tests for `decoy_engine.quality.dp.fit_dp_snapshot` (DPS Scope B).

Supersedes the Option A `apply_dp_noise` suite entirely (that mechanism
is deleted). Covers the guide's named assertions for step 5 section 3
(config validation, release ID minting, disclosure-channel regressions,
categorical order/other_count derivation) plus the section 7.2 unseeded
statistical mechanism tests.
"""

from __future__ import annotations

import decimal
import json
import logging
import math
import warnings
from collections import Counter
from collections.abc import Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.quality.carriers import CarrierError, decode_flag, decode_number, decode_text
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

    Phase 7 retired `dp_normalize.py`, the module this class used to pin
    directly at the `_normalize_numeric`/`_normalize_categorical` seam: that
    coverage now lives in `test_carriers.py` against the carrier codecs that
    replaced it (`decode_number`/`decode_text`), proven equal-strength before
    the deletion (see the phase-7 removal commit). What remains here are the
    tests that exercise the real `fit_dp_snapshot` pipeline end to end rather
    than a normalizer function directly -- the policy artifact, and totality
    against hostile/unfetchable content."""

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
        #
        # Phase 8: the categorical lines above described the retired
        # `dp_normalize` stringification path (a bool/number/decimal cell
        # rendered as a label). DPS-CODEC replaced that with declared typed
        # carriers -- `text` releases only genuine `str` cells and `flag`
        # releases the canonical `true`/`false` tokens -- so the pin below is
        # the carrier-accurate text, and the KNOWN GAP this test used to carry
        # is closed.
        assert blocks[0] == {
            "categorical_labels": (
                "a declared text column releases only genuine str cells, kept verbatim "
                "unless the value AS RECEIVED contains NUL or cannot be encoded as UTF-8 "
                "(numpy fixed-width string storage strips a trailing NUL before the fit "
                "sees it); a declared flag column releases the two canonical tokens 'true' "
                "and 'false'. The carrier declared for the column drives this, not how "
                "pandas happens to store the column"
            ),
            "categorical_unsupported": (
                "released as null: in a text column every non-str cell (a boolean, any "
                "number, decimal or complex value, a datetime or timedelta, a container, and "
                "any other type); in a flag column every non-boolean cell; and in either, "
                "text whose value AS RECEIVED carries NUL or is not UTF-8 encodable"
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
        # Each numeric-unsupported/numeric-values path the policy names, pinned
        # against the carrier codec that implements it (phase 7 retired the
        # `dp_normalize._normalize_numeric` oracle these used to call; the
        # golden vectors in test_dp_codec_golden.py pin that `decode_number`
        # matches it byte-for-byte on the values shared here).
        assert decode_number([1], lower=0.0, upper=10.0)[1] is False
        assert decode_number(pd.Timestamp("2020-01-01"), lower=0.0, upper=10.0)[1] is False
        assert decode_number(1 + 2j, lower=0.0, upper=10.0)[1] is False
        assert decode_number(object(), lower=0.0, upper=10.0)[1] is False
        assert decode_number(float("nan"), lower=0.0, upper=10.0)[1] is False
        assert decode_number(99.0, lower=0.0, upper=10.0) == (10.0, True)
        assert decode_number(float("-inf"), lower=0.0, upper=10.0) == (0.0, True)

        # Each claim the pinned prose makes about the `text` and `flag`
        # carriers, exercised against the real codec -- not just pinned text
        # (Codex round 9's own complaint about the pre-carrier version of
        # this test, now applied to the carrier-accurate replacement).
        assert decode_text("a") == ("a", True)  # text verbatim
        assert decode_text("a\x00b")[1] is False  # NUL drops
        assert decode_text("\ud800")[1] is False  # surrogate drops
        assert decode_text(pd.Timestamp("2020-01-01"))[1] is False  # temporal drops
        assert decode_text(True)[1] is False  # a bool cell is not a str: dropped
        assert decode_text(1)[1] is False  # a number cell is not a str: dropped
        assert decode_text(decimal.Decimal("1.5"))[1] is False  # nor a decimal
        assert decode_flag(True) == (True, True)
        assert decode_flag(False) == (False, True)
        assert decode_flag(2)[1] is False  # an int outside {0, 1} is not a flag value
        assert decode_flag("true")[1] is False  # text is never a flag value

        # The two canonical release tokens themselves, end to end through a
        # real fit: a `flag` column's retained category serializes as
        # `"true"`/`"false"`, never `str(bool)`'s `"True"`/`"False"` and never
        # `"1"`/`"0"` (guide section 3.4; full end-to-end coverage lives in
        # `test_dp_flag_e2e.py`).
        flag_snap = _real_fit_dp_snapshot(
            pd.DataFrame({"f": [True] * 190 + [False] * 10}),
            {"f": {"kind": "categorical", "carrier": "flag"}},
            epsilon=8.0,
            delta=1e-6,
        )
        flag_tokens = {entry["value"] for entry in flag_snap["columns"]["f"]["stats"]["top_values"]}
        assert flag_tokens
        assert flag_tokens <= {"true", "false"}

    def test_categorical_labels_that_cannot_be_encoded_are_dropped_not_raised(self):
        """C-B4, second location: a lone surrogate is a valid Python
        `str`, so `str()` succeeds and normalization used to pass it
        through. It then raised `UnicodeEncodeError` where the label
        crossed into OpenDP -- the same fit-success channel one layer
        down. Totality has to hold against the release boundary.

        The codec-level drop (`decode_text("\\ud800")`) is pinned directly in
        `test_carriers.py::TestRegressionSeeds.test_nul_and_surrogate_text`;
        what this test proves is that the REAL fit, end to end, still
        succeeds on a surrogate-bearing column rather than raising."""
        snap = fit_dp_snapshot(
            pd.DataFrame({"c": ["a", "\ud800", "b"]}, dtype=object),
            categorical_columns=["c"],
            numeric_domains={},
            epsilon=2.0,
            delta=1e-6,
        )
        assert "c" in snap["columns"]

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
        # Pinned rather than computed via the retired `dp_normalize` oracle
        # (phase 7): "age" drops the NaN and the None, clamps 200.0 to the
        # 120.0 bound; "cat" drops the None.
        expected_numeric = [1.5, 120.0, 50.25]
        expected_cat = ["a", "b", "a", "c"]
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
