"""Sprint B1a: pure schema-derived peak-memory estimator (`_mem_estimate.py` +
`_mem_estimate_schema.py`). OOM-avoidance routing redesign,
docs/plans/2026-07-10-oom-avoidance-routing-redesign.md §3.2/§3.3/§3.6, §11.

No routing wiring here -- `decide_execution_route` and the reject constants
are untouched (Sprint B1b). These tests pin: the fixed-width dtype cost
table; string-width sourcing (declared / sampled / provider-metadata /
UNPRICEABLE); the B1-calibrated `k_full_frame` constant against the real B1
peak-RSS sweep; the sequential path's cardinality (not raw-bytes) shape; the
`fits` asymmetric-margin boundary; and UNPRICEABLE propagation through
`estimate_peak_bytes` and `fits`.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.config._tables import GenerateColumnConfig, TableConfig
from decoy_engine.execution._mem_estimate import (
    K_FULL_FRAME_COLD_START,
    ColumnSizeSpec,
    FkCardinalityInput,
    TableSizeSpec,
    default_fk_key_size_bytes,
    estimate_peak_bytes,
    fits,
    is_fixed_width_dtype,
    raw_data_bytes,
)
from decoy_engine.execution._mem_estimate_schema import (
    generate_column_width_bytes,
    sample_average_string_bytes,
    table_size_spec_from_generate_table,
    table_size_spec_from_profile,
)
from decoy_engine.profile._types import ColumnProfile, TableProfile

_MB = 1024 * 1024
_GB = 1024 * _MB


def _col_profile(name: str, *, dtype: str, row_count: int = 10) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=row_count,
        null_count=0,
        distinct_count=row_count,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


# ---------------------------------------------------------------------------
# Fixed-width dtype cost table
# ---------------------------------------------------------------------------


class TestFixedWidthDtypeCosts:
    @pytest.mark.parametrize(
        ("dtype", "bytes_per_row"),
        [
            ("int64", 8),
            ("uint64", 8),
            ("float64", 8),
            ("datetime64[ns]", 8),
            ("int32", 4),
            ("float32", 4),
            ("int16", 2),
            ("uint16", 2),
            ("int8", 1),
            ("bool", 1),
        ],
    )
    def test_each_fixed_width_dtype_prices_its_known_itemsize(
        self, dtype: str, bytes_per_row: int
    ) -> None:
        table = TableSizeSpec(
            name="t", row_count=1_000, columns=(ColumnSizeSpec(name="c", dtype=dtype),)
        )
        result = raw_data_bytes((table,))
        assert result.priceable_bytes == 1_000 * bytes_per_row
        assert result.is_priceable

    def test_is_fixed_width_dtype_reports_true_for_numeric_false_for_object(self) -> None:
        assert is_fixed_width_dtype("int64") is True
        assert is_fixed_width_dtype("object") is False

    def test_unrecognized_dtype_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="unrecognized dtype"):
            ColumnSizeSpec(name="c", dtype="some_future_dtype")

    def test_multiple_fixed_width_columns_sum_per_row(self) -> None:
        table = TableSizeSpec(
            name="t",
            row_count=100,
            columns=(
                ColumnSizeSpec(name="a", dtype="int64"),
                ColumnSizeSpec(name="b", dtype="float32"),
                ColumnSizeSpec(name="c", dtype="bool"),
            ),
        )
        result = raw_data_bytes((table,))
        # 8 + 4 + 1 = 13 bytes/row
        assert result.priceable_bytes == 100 * 13


# ---------------------------------------------------------------------------
# String width sourcing: declared / sampled / provider-metadata / UNPRICEABLE
# ---------------------------------------------------------------------------


class TestStringWidthSourcing:
    def test_declared_width_prices_object_column_plus_overhead(self) -> None:
        table = TableSizeSpec(
            name="t",
            row_count=1_000,
            columns=(ColumnSizeSpec(name="s", dtype="object", string_width_bytes=12.0),),
        )
        result = raw_data_bytes((table,))
        # 12 declared chars + 57-byte object overhead (8 pointer + 49 CPython header).
        assert result.priceable_bytes == 1_000 * (12 + 57)

    def test_unpriceable_column_is_excluded_from_priceable_bytes_and_named(self) -> None:
        table = TableSizeSpec(
            name="t",
            row_count=1_000,
            columns=(
                ColumnSizeSpec(name="priced", dtype="int64"),
                ColumnSizeSpec(name="free_text", dtype="object", unpriceable=True),
            ),
        )
        result = raw_data_bytes((table,))
        assert result.priceable_bytes == 1_000 * 8
        assert result.unpriceable_columns == (("t", "free_text"),)
        assert not result.is_priceable

    def test_variable_width_without_declared_width_or_unpriceable_flag_rejected(self) -> None:
        with pytest.raises(ValueError, match="string_width_bytes"):
            ColumnSizeSpec(name="c", dtype="object")

    def test_unpriceable_and_string_width_bytes_are_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            ColumnSizeSpec(name="c", dtype="object", string_width_bytes=10.0, unpriceable=True)

    def test_sample_average_string_bytes_measures_real_utf8_length(self) -> None:
        column = pa.array(["ab", "abcd", None, "abcdef"])
        # non-null lengths: 2, 4, 6 -> mean 4.0
        assert sample_average_string_bytes(column) == pytest.approx(4.0)

    def test_sample_average_string_bytes_all_null_is_zero(self) -> None:
        column = pa.array([None, None], type=pa.string())
        assert sample_average_string_bytes(column) == 0.0

    def test_table_size_spec_from_profile_uses_declared_width_override(self) -> None:
        profile_table = TableProfile(
            name="t",
            row_count=5,
            columns=(_col_profile("s", dtype="object", row_count=5),),
        )
        spec = table_size_spec_from_profile(profile_table, declared_widths={"s": 20.0})
        assert spec.columns[0].string_width_bytes == 20.0
        assert not spec.columns[0].unpriceable

    def test_table_size_spec_from_profile_samples_when_no_declared_width(self) -> None:
        profile_table = TableProfile(
            name="t",
            row_count=3,
            columns=(_col_profile("s", dtype="object", row_count=3),),
        )
        sample_col = pa.array(["a", "bb", "ccc"])
        spec = table_size_spec_from_profile(profile_table, sample={"s": sample_col})
        assert spec.columns[0].string_width_bytes == pytest.approx(2.0)

    def test_table_size_spec_from_profile_marks_unpriceable_with_no_width_source(self) -> None:
        profile_table = TableProfile(
            name="t",
            row_count=3,
            columns=(_col_profile("s", dtype="object", row_count=3),),
        )
        spec = table_size_spec_from_profile(profile_table)
        assert spec.columns[0].unpriceable

    def test_table_size_spec_from_profile_prices_fixed_width_columns_directly(self) -> None:
        profile_table = TableProfile(
            name="t",
            row_count=3,
            columns=(_col_profile("n", dtype="int64", row_count=3),),
        )
        spec = table_size_spec_from_profile(profile_table)
        assert spec.columns[0].dtype == "int64"
        assert not spec.columns[0].unpriceable


# ---------------------------------------------------------------------------
# Generation columns: provider/strategy metadata, not sampling (§3.2b/§11)
# ---------------------------------------------------------------------------


class TestGenerateColumnWidth:
    def test_known_faker_type_prices_from_metadata_table(self) -> None:
        col = GenerateColumnConfig(name="e", type="faker", faker_type="email")
        assert generate_column_width_bytes(col) == pytest.approx(24.0)

    def test_unknown_faker_type_is_unpriceable(self) -> None:
        col = GenerateColumnConfig(name="e", type="faker", faker_type="some_new_provider")
        assert generate_column_width_bytes(col) is None

    def test_categorical_prices_from_widest_declared_category(self) -> None:
        col = GenerateColumnConfig(name="cat", type="categorical", categories=["a", "bb", "ccc"])
        assert generate_column_width_bytes(col) == pytest.approx(3.0)

    def test_formula_type_is_unpriceable_no_guess(self) -> None:
        col = GenerateColumnConfig(name="f", type="formula", formula="1+1")
        assert generate_column_width_bytes(col) is None

    def test_table_size_spec_from_generate_table_prices_sequence_as_int64(self) -> None:
        table = TableConfig(
            name="t",
            row_count=100,
            generate_columns=[GenerateColumnConfig(name="id", type="sequence", start=0)],
        )
        spec = table_size_spec_from_generate_table(table)
        assert spec.row_count == 100
        assert spec.columns[0].dtype == "int64"
        assert not spec.columns[0].unpriceable

    def test_table_size_spec_from_generate_table_prices_known_faker_column(self) -> None:
        table = TableConfig(
            name="t",
            row_count=100,
            generate_columns=[GenerateColumnConfig(name="e", type="faker", faker_type="email")],
        )
        spec = table_size_spec_from_generate_table(table)
        assert spec.columns[0].string_width_bytes == pytest.approx(24.0)

    def test_table_size_spec_from_generate_table_marks_free_text_unpriceable(self) -> None:
        table = TableConfig(
            name="t",
            row_count=100,
            generate_columns=[
                GenerateColumnConfig(name="bio", type="statistical", snapshot_file="s.json")
            ],
        )
        spec = table_size_spec_from_generate_table(table)
        assert spec.columns[0].unpriceable

    def test_table_size_spec_from_generate_table_requires_row_count(self) -> None:
        # A mask table has no row_count; building a generate spec from it is
        # a caller error, not a silent zero-row estimate.
        table = TableConfig(
            name="t", columns=[{"name": "c", "strategy": "hash", "provider": "hash"}]
        )
        with pytest.raises(ValueError, match="row_count"):
            table_size_spec_from_generate_table(table)


# ---------------------------------------------------------------------------
# B1 calibration: k_full_frame reproduces the real full_frame sweep
# ---------------------------------------------------------------------------


def _avg_positional_key_len(n: int) -> float:
    """Exact average length of a 1-char prefix + base-10 str(i) for i in
    [0, n): the same key shape `tests/perf_fixtures/fk_relational.py`
    `build_table` uses for its "p"/"c"-prefixed positional keys. Closed-form
    (not sampled) so the calibration schema below is reproducible without
    materializing millions of strings."""
    if n <= 0:
        return 1.0
    total_digits = 0
    digit_len = 1
    while True:
        start = 0 if digit_len == 1 else 10 ** (digit_len - 1)
        end = 10**digit_len
        if start >= n:
            break
        count = min(end, n) - start
        total_digits += count * digit_len
        digit_len += 1
    return 1.0 + total_digits / n


def _b1_calibration_tables(rows_per_table: int) -> tuple[TableSizeSpec, ...]:
    """The exact B1 full_frame calibration schema: parent/child/grandchild,
    16 payload columns/table (width=12, the fixture's `_string_pool` width),
    plus each table's positional string key column(s) --
    tests/perf_fixtures/fk_relational.py `build_table` (width=16 default).
    """
    key_width = _avg_positional_key_len(rows_per_table)
    payload_cols = tuple(
        ColumnSizeSpec(name=f"payload_{i:02d}", dtype="object", string_width_bytes=12.0)
        for i in range(16)
    )

    def _key(name: str) -> ColumnSizeSpec:
        return ColumnSizeSpec(name=name, dtype="object", string_width_bytes=key_width)

    parent = TableSizeSpec(
        name="parent", row_count=rows_per_table, columns=(_key("id"), *payload_cols)
    )
    child = TableSizeSpec(
        name="child",
        row_count=rows_per_table,
        columns=(_key("id"), _key("parent_id"), *payload_cols),
    )
    grandchild = TableSizeSpec(
        name="grandchild", row_count=rows_per_table, columns=(_key("child_id"), *payload_cols)
    )
    return (parent, child, grandchild)


# rows/table -> measured B1 full_frame peak RSS in MB (design doc §1/§3.2).
_B1_MEASURED_PEAK_MB = {
    1_000_000: 4_448,
    2_000_000: 8_474,
    4_000_000: 16_560,
    6_000_000: 24_768,
}


class TestFullFrameCalibration:
    def test_raw_data_bytes_reproduces_the_derivation_at_6m_rows(self) -> None:
        tables = _b1_calibration_tables(6_000_000)
        result = raw_data_bytes(tables)
        assert result.is_priceable
        # Pinned in the module docstring: ~21.428 GB at 6M rows/table.
        assert result.priceable_bytes == pytest.approx(21_427_555_560, rel=1e-6)

    def test_pinned_k_full_frame_predicts_the_6m_peak_within_15_percent(self) -> None:
        tables = _b1_calibration_tables(6_000_000)
        raw = raw_data_bytes(tables)
        predicted_bytes = raw.priceable_bytes * K_FULL_FRAME_COLD_START
        measured_bytes = _B1_MEASURED_PEAK_MB[6_000_000] * _MB
        assert predicted_bytes == pytest.approx(measured_bytes, rel=0.15)

    @pytest.mark.parametrize("rows_per_table", sorted(_B1_MEASURED_PEAK_MB))
    def test_pinned_k_full_frame_predicts_every_b1_sweep_point_within_15_percent(
        self, rows_per_table: int
    ) -> None:
        """The single COLD-START constant, calibrated at the 6M anchor,
        still predicts the smaller B1 points within the stated tolerance --
        this is the linearity claim (§1) that justifies a constant
        multiplier at all, not a per-row-count-tier lookup."""
        tables = _b1_calibration_tables(rows_per_table)
        raw = raw_data_bytes(tables)
        predicted_bytes = raw.priceable_bytes * K_FULL_FRAME_COLD_START
        measured_bytes = _B1_MEASURED_PEAK_MB[rows_per_table] * _MB
        assert predicted_bytes == pytest.approx(measured_bytes, rel=0.15)

    def test_k_derived_from_a_small_point_extrapolates_to_the_large_point(self) -> None:
        """Derive k from the SMALLEST B1 sample (1M rows/table, the worst
        case per §11's small-probe-overestimate warning) and confirm it
        still extrapolates to the largest (6M) within tolerance -- a genuine
        extrapolation check, not a same-point tautology."""
        small = _b1_calibration_tables(1_000_000)
        raw_small = raw_data_bytes(small)
        k_from_small = (_B1_MEASURED_PEAK_MB[1_000_000] * _MB) / raw_small.priceable_bytes

        large = _b1_calibration_tables(6_000_000)
        raw_large = raw_data_bytes(large)
        predicted = raw_large.priceable_bytes * k_from_small
        measured = _B1_MEASURED_PEAK_MB[6_000_000] * _MB
        assert predicted == pytest.approx(measured, rel=0.15)

    def test_estimate_peak_bytes_full_frame_matches_raw_times_k(self) -> None:
        tables = _b1_calibration_tables(1_000_000)
        estimate = estimate_peak_bytes(tables, "full_frame")
        raw = raw_data_bytes(tables)
        assert not estimate.unpriceable
        assert estimate.estimated_bytes == int(raw.priceable_bytes * K_FULL_FRAME_COLD_START)


# ---------------------------------------------------------------------------
# Sequential: O(cardinality), not raw_bytes * k (§11 §3.2a)
# ---------------------------------------------------------------------------


class TestSequentialCardinality:
    def _one_table(self, row_count: int) -> TableSizeSpec:
        return TableSizeSpec(
            name="t",
            row_count=row_count,
            columns=(ColumnSizeSpec(name="s", dtype="object", string_width_bytes=12.0),),
        )

    def test_sequential_estimate_does_not_scale_with_table_count(self) -> None:
        """The defining structural difference from full_frame/out_of_core:
        adding more equally-sized tables must NOT multiply the sequential
        estimate, because sequential streams one table at a time -- it is
        never all of them summed. full_frame, by contrast, DOES scale with
        table count (it holds everything resident at once)."""
        one_table = (self._one_table(1_000),)
        three_tables = (self._one_table(1_000),) * 3

        seq_one = estimate_peak_bytes(one_table, "sequential")
        seq_three = estimate_peak_bytes(three_tables, "sequential")
        assert seq_one.estimated_bytes == seq_three.estimated_bytes

        full_one = estimate_peak_bytes(one_table, "full_frame")
        full_three = estimate_peak_bytes(three_tables, "full_frame")
        # int()-truncation on the combined sum vs. 3x a single truncated
        # estimate can differ by a rounding hair; the scaling claim itself
        # (not exact integer equality) is what matters here.
        assert full_three.estimated_bytes == pytest.approx(3 * full_one.estimated_bytes, rel=1e-4)

    def test_sequential_estimate_scales_with_distinct_fk_key_count_not_row_count(self) -> None:
        table = (self._one_table(1_000_000),)
        low_cardinality = FkCardinalityInput(distinct_key_count=10, key_size_bytes=64)
        high_cardinality = FkCardinalityInput(distinct_key_count=1_000_000, key_size_bytes=64)

        low = estimate_peak_bytes(table, "sequential", fk_cardinality=low_cardinality)
        high = estimate_peak_bytes(table, "sequential", fk_cardinality=high_cardinality)
        assert high.estimated_bytes > low.estimated_bytes

    def test_sequential_without_fk_cardinality_still_prices_the_working_set(self) -> None:
        table = (self._one_table(1_000),)
        estimate = estimate_peak_bytes(table, "sequential")
        assert not estimate.unpriceable
        assert estimate.estimated_bytes > 0

    def test_default_fk_key_size_bytes_includes_hash_entry_overhead(self) -> None:
        # A zero-width key still costs the object overhead + hash-slot overhead.
        assert default_fk_key_size_bytes(0.0) > 0
        assert default_fk_key_size_bytes(20.0) > default_fk_key_size_bytes(0.0)

    def test_unknown_path_raises(self) -> None:
        table = (self._one_table(10),)
        with pytest.raises(ValueError, match="unknown execution path"):
            estimate_peak_bytes(table, "not_a_real_path")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fits(): asymmetric margin boundary (§3.6)
# ---------------------------------------------------------------------------


class TestFitsMargin:
    def _table(self, row_count: int) -> tuple[TableSizeSpec, ...]:
        return (
            TableSizeSpec(
                name="t", row_count=row_count, columns=(ColumnSizeSpec(name="n", dtype="int64"),)
            ),
        )

    def test_just_fits_under_the_margin(self) -> None:
        tables = self._table(1_000)
        estimate = estimate_peak_bytes(tables, "full_frame").estimated_bytes
        # Budget set exactly at the margin boundary plus a hair of slack.
        budget = int(estimate * 1.30) + 1
        assert fits(tables, "full_frame", budget) is True

    def test_just_over_the_margin_does_not_fit(self) -> None:
        tables = self._table(1_000)
        estimate = estimate_peak_bytes(tables, "full_frame").estimated_bytes
        budget = int(estimate * 1.30) - 1
        assert fits(tables, "full_frame", budget) is False

    def test_raw_fit_without_margin_is_not_enough_to_pass(self) -> None:
        """A budget that covers the bare estimate but not the (1+error_band)
        margin must still fail -- this is the asymmetric-margin point of
        §3.6, not a bare `estimate < budget` check."""
        tables = self._table(1_000)
        estimate = estimate_peak_bytes(tables, "full_frame").estimated_bytes
        budget = estimate + 1  # covers the bare estimate, not the 30% margin
        assert fits(tables, "full_frame", budget) is False

    def test_custom_error_band_widens_or_narrows_the_margin(self) -> None:
        tables = self._table(1_000)
        estimate = estimate_peak_bytes(tables, "full_frame").estimated_bytes
        budget = int(estimate * 1.10)
        assert fits(tables, "full_frame", budget, error_band=0.05) is True
        assert fits(tables, "full_frame", budget, error_band=0.50) is False

    def test_negative_error_band_rejected(self) -> None:
        with pytest.raises(ValueError, match="error_band"):
            fits(self._table(10), "full_frame", 1_000, error_band=-0.1)

    def test_non_positive_budget_rejected(self) -> None:
        with pytest.raises(ValueError, match="budget_bytes"):
            fits(self._table(10), "full_frame", 0)


# ---------------------------------------------------------------------------
# UNPRICEABLE propagation (§3.5)
# ---------------------------------------------------------------------------


class TestUnpriceablePropagation:
    def _tables_with_unpriceable_column(self) -> tuple[TableSizeSpec, ...]:
        return (
            TableSizeSpec(
                name="t",
                row_count=1_000,
                columns=(
                    ColumnSizeSpec(name="n", dtype="int64"),
                    ColumnSizeSpec(name="bio", dtype="object", unpriceable=True),
                ),
            ),
        )

    def test_estimate_peak_bytes_full_frame_returns_none_and_names_the_column(self) -> None:
        tables = self._tables_with_unpriceable_column()
        estimate = estimate_peak_bytes(tables, "full_frame")
        assert estimate.unpriceable
        assert estimate.estimated_bytes is None
        assert estimate.unpriceable_columns == (("t", "bio"),)

    def test_estimate_peak_bytes_out_of_core_also_propagates(self) -> None:
        tables = self._tables_with_unpriceable_column()
        estimate = estimate_peak_bytes(tables, "out_of_core")
        assert estimate.unpriceable

    def test_estimate_peak_bytes_sequential_also_propagates(self) -> None:
        tables = self._tables_with_unpriceable_column()
        estimate = estimate_peak_bytes(tables, "sequential")
        assert estimate.unpriceable
        assert estimate.unpriceable_columns == (("t", "bio"),)

    def test_fits_returns_none_not_a_boolean_when_unpriceable(self) -> None:
        tables = self._tables_with_unpriceable_column()
        assert fits(tables, "full_frame", 10 * _GB) is None

    def test_peak_estimate_cannot_be_constructed_none_bytes_without_a_reason(self) -> None:
        from decoy_engine.execution._mem_estimate import PeakEstimate

        with pytest.raises(ValueError, match="unpriceable_columns"):
            PeakEstimate(estimated_bytes=None, unpriceable_columns=())
