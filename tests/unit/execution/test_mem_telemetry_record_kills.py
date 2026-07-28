"""Mutation kill-tests for the RECORD + HELPER functions of `_mem_telemetry.py`
(TQ isolated-substrate grade, branch `tq/isolated-substrate-grade`).

One test per surviving LOGIC mutant, grouped by function, asserting the EXACT
machine field the mutation breaks (a percentile-interpolation value, a
width-class bucket boundary, a fingerprint field/length, a telemetry-record
field, or the #74 basis-contract's FK term). Message-prose survivors on
reachable-but-non-contract error branches, unreachable defensive branches, and
byte-identical relabels are adjudicated in the module ledger, not here.

Fixtures mirror `tests/unit/execution/test_mem_telemetry.py`.
"""

from __future__ import annotations

import pytest

from decoy_engine.execution._governor import GovernorTripRecord
from decoy_engine.execution._isolated_common import IsolatedRunResult
from decoy_engine.execution._mem_estimate import (
    ColumnSizeSpec,
    FkCardinalityInput,
    TableSizeSpec,
    estimator_basis_bytes,
)

# `_percentile` and `_width_class` are private helpers with their own contracts
# (numpy's default linear-interpolation percentile; the width-bucket ladder).
# The public `recalibrate_k` only ever calls `_percentile` at pct=1.0, which
# never reaches the interpolation tail, so that mutant set is only reachable by
# testing the helper directly. `_width_class` is exercised through the public
# `schema_fingerprint` below.
from decoy_engine.execution._mem_telemetry import (
    _percentile,
    schema_fingerprint,
    telemetry_record_from_governor_trip,
    telemetry_record_from_isolated_run,
)

_MB = 1024 * 1024
_GB = 1024 * _MB


def _table(name: str, row_count: int, columns: tuple[ColumnSizeSpec, ...]) -> TableSizeSpec:
    return TableSizeSpec(name=name, row_count=row_count, columns=columns)


def _isolated_result(
    *, peak_rss_mb: float | None = 1024.0, outcome: str = "completed", isolated: bool = True
) -> IsolatedRunResult:
    return IsolatedRunResult(
        outcome=outcome,  # type: ignore[arg-type]
        peak_rss_mb=peak_rss_mb,
        outputs={} if outcome == "completed" else None,
        quality_metrics={},
        table_kinds={},
        returncode=0,
        signal_number=None,
        error=None,
        isolated=isolated,
    )


def _three_tables() -> tuple[TableSizeSpec, ...]:
    # Three DIFFERENT sizes so the sequential working set (two largest) is not
    # the total, and the FK dedup term is separable from the working set.
    return (
        _table("big", 1_000_000, (ColumnSizeSpec(name="a", dtype="int64"),)),
        _table("mid", 500_000, (ColumnSizeSpec(name="a", dtype="int64"),)),
        _table("small", 100_000, (ColumnSizeSpec(name="a", dtype="int64"),)),
    )


# ---------------------------------------------------------------------------
# _percentile -- numpy's default linear-interpolation method. Tested directly:
# the public path pins pct=1.0 (the exact max), which never reaches the
# interpolation tail these mutants live in.
# ---------------------------------------------------------------------------


class TestPercentileInterpolation:
    def test_hi_index_is_lo_plus_one_not_minus_one(self) -> None:
        # mut17: `hi = min(lo + 1, ...)` -> `min(lo - 1, ...)`. At pct=0.25 on a
        # 3-element list lo=0; the upper neighbour must be ordered[1]=1.0, not
        # ordered[-1]=5.0. Correct interpolation is 0 + (1-0)*0.5 = 0.5.
        assert _percentile([0.0, 1.0, 5.0], 0.25) == pytest.approx(0.5)

    def test_hi_index_is_lo_plus_one_not_plus_two(self) -> None:
        # mut18: `+ 1` -> `+ 2`. Same anchor: the neighbour is ordered[1], not
        # ordered[2]=5.0 (which would give 2.5).
        assert _percentile([0.0, 1.0, 5.0], 0.25) == pytest.approx(0.5)

    def test_hi_clamps_to_last_index_not_second_to_last(self) -> None:
        # mut20: `min(lo + 1, len - 1)` -> `min(lo + 1, len - 2)`. At pct=0.75
        # lo=1 and the neighbour must be ordered[2]=5.0: 1 + (5-1)*0.5 = 3.0.
        # Clamping to len-2=1 would collapse it to ordered[1]=1.0.
        assert _percentile([0.0, 1.0, 5.0], 0.75) == pytest.approx(3.0)

    def test_frac_is_the_fractional_part_of_the_index(self) -> None:
        # mut22: `frac = idx - lo` -> `frac = None`, which makes the final
        # `... * frac` raise TypeError. A finite result at all kills it; the
        # value pins the correct fraction too.
        assert _percentile([0.0, 1.0, 5.0], 0.25) == pytest.approx(0.5)

    def test_frac_subtracts_lo_not_adds_it(self) -> None:
        # mut23: `idx - lo` -> `idx + lo`. At pct=0.75 idx=1.5, lo=1: frac must
        # be 0.5 (-> 3.0), not 2.5 (-> 11.0).
        assert _percentile([0.0, 1.0, 5.0], 0.75) == pytest.approx(3.0)

    def test_interpolation_adds_the_weighted_gap(self) -> None:
        # mut24: `ordered[lo] + (gap) * frac` -> `ordered[lo] - (gap) * frac`.
        # 0 + (1-0)*0.5 = 0.5, never -0.5.
        assert _percentile([0.0, 1.0, 5.0], 0.25) == pytest.approx(0.5)

    def test_interpolation_scales_the_gap_by_frac(self) -> None:
        # mut25: `(gap) * frac` -> `(gap) / frac`. 0 + (1-0)*0.5 = 0.5, not
        # (1-0)/0.5 = 2.0.
        assert _percentile([0.0, 1.0, 5.0], 0.25) == pytest.approx(0.5)

    def test_gap_is_hi_minus_lo_not_hi_plus_lo(self) -> None:
        # mut26: `(ordered[hi] - ordered[lo])` -> `(ordered[hi] + ordered[lo])`.
        # At pct=0.75 the gap is (5-1)=4 -> 3.0, not (5+1)=6 -> 4.0.
        assert _percentile([0.0, 1.0, 5.0], 0.75) == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# _width_class -- the string-width bucket ladder. Exercised through the public
# `schema_fingerprint`, which folds each column's width class into the hash.
# ---------------------------------------------------------------------------


class TestWidthClassBoundary:
    def test_bucket_boundary_is_inclusive(self) -> None:
        # mut4: `if width_bytes <= boundary` -> `< boundary`. A width sitting
        # EXACTLY on a boundary (16.0) belongs to that bucket ("<=16"); 17.0
        # belongs to the next ("<=32"). Inclusive `<=` keeps them distinct, so
        # their fingerprints differ. The mutant's strict `<` pushes 16.0 up into
        # "<=32", colliding it with 17.0 -- the fingerprints would then match.
        on_boundary = (
            _table("t", 1, (ColumnSizeSpec(name="s", dtype="object", string_width_bytes=16.0),)),
        )
        just_above = (
            _table("t", 1, (ColumnSizeSpec(name="s", dtype="object", string_width_bytes=17.0),)),
        )
        assert schema_fingerprint(on_boundary) != schema_fingerprint(just_above)


# ---------------------------------------------------------------------------
# schema_fingerprint -- the stable per-shape hash.
# ---------------------------------------------------------------------------


class TestSchemaFingerprint:
    def test_fk_edge_hashes_its_position_pair_not_a_constant(self) -> None:
        # mut16: `fk_positions.append((child_idx, parent_idx))` -> `.append(None)`.
        # Two schemas with IDENTICAL table shapes and the SAME number of FK edges
        # but DIFFERENT endpoints must fingerprint differently -- the endpoints
        # are part of the shape. Appending a constant `None` erases the endpoints,
        # collapsing both to the same hash.
        col = (ColumnSizeSpec(name="id", dtype="int64"),)
        tables = (_table("t0", 1, col), _table("t1", 1, col), _table("t2", 1, col))
        fp_edge_a = schema_fingerprint(tables, fk_edges=(("t1", "t0"),))
        fp_edge_b = schema_fingerprint(tables, fk_edges=(("t2", "t0"),))
        assert fp_edge_a != fp_edge_b

    def test_fingerprint_is_truncated_to_sixteen_hex_chars(self) -> None:
        # mut25: `hexdigest()[:16]` -> `[:17]`. The fingerprint width is a fixed
        # 16 hex chars.
        col = (ColumnSizeSpec(name="id", dtype="int64"),)
        assert len(schema_fingerprint((_table("t", 1, col),))) == 16


# ---------------------------------------------------------------------------
# telemetry_record_from_governor_trip -- record-field population + the #74
# basis-contract call that a governor trip routes through.
# ---------------------------------------------------------------------------


class TestGovernorTripRecordFields:
    def _trip(self, route: str = "full_frame") -> GovernorTripRecord:
        return GovernorTripRecord(
            route=route,  # type: ignore[arg-type]
            budget_bytes=8 * _GB,
            observed_peak_mb=9000.0,
            trip_kind="governor_kill",
            reroute_to="out_of_core",
            error=None,
        )

    def test_record_carries_the_passed_fingerprint_and_governor_trip_outcome(self) -> None:
        # mut29: `schema_fingerprint=schema_fingerprint` -> `None`.
        # mut35: `outcome="governor_trip"` -> `None`.
        # mut44/45: the `"governor_trip"` literal is XX-wrapped / upper-cased.
        record = telemetry_record_from_governor_trip(
            self._trip(),
            schema_fingerprint="fp-gov",
            raw_bytes=1 * _GB,
            predicted_bytes=3 * _GB,
        )
        assert record.schema_fingerprint == "fp-gov"
        assert record.outcome == "governor_trip"

    def test_sequential_trip_without_tables_uses_the_real_route_in_the_basis_check(self) -> None:
        # mut8: the basis-check call's `path=trip.route` -> `path=None`. A
        # sequential trip WITHOUT `tables` must fail the #74 contract (sequential
        # needs the working-set basis). Passing `None` as the path skips the
        # sequential branch entirely, silently accepting the wrong basis.
        with pytest.raises(ValueError, match="basis contract"):
            telemetry_record_from_governor_trip(
                self._trip("sequential"),
                schema_fingerprint="fp",
                raw_bytes=12_000_000,
                predicted_bytes=3 * _GB,
            )

    def test_basis_check_receives_the_real_raw_bytes(self) -> None:
        # mut9: the basis-check call's `raw_bytes=raw_bytes` -> `raw_bytes=None`.
        # A full_frame trip whose `raw_bytes` matches the estimator basis must
        # build; passing `None` to the check makes it mismatch every basis and
        # wrongly reject a correct record.
        tables = _three_tables()
        total = estimator_basis_bytes(tables, "full_frame").basis_bytes
        assert total is not None
        record = telemetry_record_from_governor_trip(
            self._trip("full_frame"),
            schema_fingerprint="fp",
            raw_bytes=total,
            predicted_bytes=3 * _GB,
            tables=tables,
        )
        assert record.raw_bytes == total

    def test_basis_check_receives_the_real_tables(self) -> None:
        # mut10: the basis-check call's `tables=tables` -> `tables=None`. A
        # sequential trip WITH tables and the correct working-set basis must
        # build; dropping `tables` re-triggers the "sequential needs tables"
        # guard and wrongly rejects it.
        tables = _three_tables()
        working_set = estimator_basis_bytes(tables, "sequential").basis_bytes
        assert working_set is not None
        record = telemetry_record_from_governor_trip(
            self._trip("sequential"),
            schema_fingerprint="fp",
            raw_bytes=working_set,
            predicted_bytes=3 * _GB,
            tables=tables,
        )
        assert record.raw_bytes == working_set

    def test_basis_check_receives_the_fk_cardinality(self) -> None:
        # mut11: the basis-check call's `fk_cardinality=fk_cardinality` -> `None`.
        # A sequential trip whose `raw_bytes` includes the FK dedup term must
        # build; dropping the FK cardinality recomputes a SMALLER working-set-only
        # basis that no longer matches, wrongly rejecting the record.
        tables = _three_tables()
        fk = FkCardinalityInput(distinct_key_count=10_000, key_size_bytes=64)
        basis_with_fk = estimator_basis_bytes(tables, "sequential", fk_cardinality=fk).basis_bytes
        assert basis_with_fk is not None
        record = telemetry_record_from_governor_trip(
            self._trip("sequential"),
            schema_fingerprint="fp",
            raw_bytes=basis_with_fk,
            predicted_bytes=3 * _GB,
            tables=tables,
            fk_cardinality=fk,
        )
        assert record.raw_bytes == basis_with_fk


# ---------------------------------------------------------------------------
# telemetry_record_from_isolated_run -- record-field population + the #74
# basis-contract call.
# ---------------------------------------------------------------------------


class TestIsolatedRunRecordFields:
    def test_record_carries_the_passed_fingerprint(self) -> None:
        # mut47: `schema_fingerprint=schema_fingerprint` -> `None`.
        record = telemetry_record_from_isolated_run(
            _isolated_result(),
            schema_fingerprint="fp-iso",
            path="full_frame",
            raw_bytes=1 * _GB,
            predicted_bytes=3 * _GB,
        )
        assert record.schema_fingerprint == "fp-iso"

    def test_basis_check_receives_the_fk_cardinality(self) -> None:
        # mut13: the basis-check call's `fk_cardinality=fk_cardinality` -> `None`.
        # As with the governor builder: a sequential record whose `raw_bytes`
        # includes the FK dedup term must build; dropping the FK cardinality
        # recomputes a smaller basis and wrongly rejects it.
        tables = _three_tables()
        fk = FkCardinalityInput(distinct_key_count=10_000, key_size_bytes=64)
        basis_with_fk = estimator_basis_bytes(tables, "sequential", fk_cardinality=fk).basis_bytes
        assert basis_with_fk is not None
        record = telemetry_record_from_isolated_run(
            _isolated_result(),
            schema_fingerprint="fp",
            path="sequential",
            raw_bytes=basis_with_fk,
            predicted_bytes=3 * _GB,
            tables=tables,
            fk_cardinality=fk,
        )
        assert record.raw_bytes == basis_with_fk


# ---------------------------------------------------------------------------
# _assert_basis_matches_estimator -- the #74 basis-contract guard. Its two
# LOGIC survivors both concern recomputing the basis WITHOUT the FK cardinality;
# both are exercised through the sequential-with-FK builder above, so a
# dedicated test pins the guard directly.
# ---------------------------------------------------------------------------


class TestAssertBasisFkTerm:
    def test_guard_recomputes_the_basis_with_the_fk_cardinality(self) -> None:
        # mut28: `estimator_basis_bytes(tables, path, fk_cardinality=fk_cardinality)`
        #        -> `fk_cardinality=None`.
        # mut31: same call drops the `fk_cardinality=` argument entirely.
        # Either way the guard verifies `raw_bytes` against a working-set-only
        # basis that excludes the FK dedup term, so a record whose `raw_bytes`
        # correctly includes that term is rejected. The record must build.
        tables = _three_tables()
        fk = FkCardinalityInput(distinct_key_count=10_000, key_size_bytes=64)
        basis_with_fk = estimator_basis_bytes(tables, "sequential", fk_cardinality=fk).basis_bytes
        working_set_only = estimator_basis_bytes(tables, "sequential").basis_bytes
        assert basis_with_fk is not None and working_set_only is not None
        assert basis_with_fk != working_set_only  # the FK term is separable
        record = telemetry_record_from_isolated_run(
            _isolated_result(),
            schema_fingerprint="fp",
            path="sequential",
            raw_bytes=basis_with_fk,
            predicted_bytes=3 * _GB,
            tables=tables,
            fk_cardinality=fk,
        )
        assert record.raw_bytes == basis_with_fk
