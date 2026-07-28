"""Mutation-kill oracles for `execution/_planner.py` (TQ substrate sweep).

These target the planner's route/strategy compatibility gates directly at the
helper level: each test drives one gate with a hand-built config / Arrow table /
work list and asserts the HARDCODED machine outcome (the rejection code, the
returned column list, the emitted reason data). Expected values are literals,
never recomputed from planner constants, so the oracles stay independent of the
code under test.

Integration-level classify_job / _chunked_rejection oracles live in
`test_execution_planner.py`; this file pins the leaf gates that classify_job
composes.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._planner import (
    _bucketize_columns,
    _fpe_join_group_columns,
    _polars_native_rejection,
    _runtime_source_rejections,
    _table_column_entries,
    _whole_column_state_rejections,
)
from decoy_engine.profile._readers import LazySource

# --------------------------------------------------------------------------
# _table_column_entries: resolve one table's raw column dicts
# --------------------------------------------------------------------------

_TWO_TABLE_CFG = {
    "tables": [
        {"name": "t1", "columns": [{"name": "a", "strategy": "hash"}]},
        {
            "name": "t2",
            "columns": [
                {"name": "b", "strategy": "bucketize"},
                {"name": "c", "strategy": "fpe", "provider_config": {"fpe_join_group": True}},
            ],
        },
    ]
}


class TestTableColumnEntries:
    def test_returns_named_columns_of_the_requested_table(self):
        entries = _table_column_entries(_TWO_TABLE_CFG, table="t2")
        assert [c.get("name") for c in entries] == ["b", "c"]

    def test_resolves_by_name_not_position(self):
        # Kills the "pick first dict table" / "name != table" mutations: t1 is
        # first, so a broken match returns t1's ['a'] instead of t2's columns.
        assert [c.get("name") for c in _table_column_entries(_TWO_TABLE_CFG, table="t1")] == ["a"]

    def test_missing_table_returns_empty_not_raises(self):
        # Kills the `next(...)` no-default mutation (StopIteration on miss).
        assert _table_column_entries(_TWO_TABLE_CFG, table="nope") == []

    def test_no_tables_key_returns_empty(self):
        assert _table_column_entries({}, table="t1") == []


# --------------------------------------------------------------------------
# _bucketize_columns: names of bucketize-strategy columns, sorted
# --------------------------------------------------------------------------


class TestBucketizeColumns:
    def test_selects_bucketize_columns_of_the_table(self):
        assert _bucketize_columns(_TWO_TABLE_CFG, table="t2") == ["b"]

    def test_non_bucketize_table_is_empty(self):
        assert _bucketize_columns(_TWO_TABLE_CFG, table="t1") == []

    def test_nameless_bucketize_column_falls_back_to_question_mark(self):
        # Kills the name-default mutations (default -> None / "XX?XX"): with the
        # `name` key absent the fallback is exactly "?".
        cfg = {"tables": [{"name": "t", "columns": [{"strategy": "bucketize"}]}]}
        assert _bucketize_columns(cfg, table="t") == ["?"]


# --------------------------------------------------------------------------
# _fpe_join_group_columns: fpe columns whose provider_config sets fpe_join_group
# --------------------------------------------------------------------------


class TestFpeJoinGroupColumns:
    def test_selects_only_fpe_columns_with_join_group_flag(self):
        # t2 has a bucketize column (non-fpe) BEFORE the fpe join-group column,
        # so `continue`->`break` is caught (it would stop before reaching 'c').
        assert _fpe_join_group_columns(_TWO_TABLE_CFG, table="t2") == ["c"]

    def test_fpe_without_join_group_flag_is_excluded(self):
        cfg = {
            "tables": [
                {"name": "t", "columns": [{"name": "c", "strategy": "fpe"}]},
            ]
        }
        assert _fpe_join_group_columns(cfg, table="t") == []

    def test_resolves_by_name_and_missing_table_is_empty(self):
        assert _fpe_join_group_columns(_TWO_TABLE_CFG, table="t1") == []
        assert _fpe_join_group_columns(_TWO_TABLE_CFG, table="nope") == []

    def test_nameless_fpe_join_group_column_falls_back_to_question_mark(self):
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [{"strategy": "fpe", "provider_config": {"fpe_join_group": True}}],
                }
            ]
        }
        assert _fpe_join_group_columns(cfg, table="t") == ["?"]

    def test_non_dict_column_entry_is_skipped(self):
        # Kills the `or`->`and` in the dict guard: a non-dict entry must be
        # skipped, not `.get(...)`-ed.
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        "not-a-dict",
                        {
                            "name": "c",
                            "strategy": "fpe",
                            "provider_config": {"fpe_join_group": True},
                        },
                    ],
                }
            ]
        }
        assert _fpe_join_group_columns(cfg, table="t") == ["c"]


# --------------------------------------------------------------------------
# _whole_column_state_rejections: when-predicate and undated-date_shift gates
# --------------------------------------------------------------------------


class TestWholeColumnStateRejections:
    def test_flags_when_and_undated_date_shift_columns(self):
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "wa", "strategy": "redact", "when": "x"},
                        {"name": "wb", "strategy": "redact", "when": "y"},
                        {"name": "da", "strategy": "date_shift"},
                        {"name": "db", "strategy": "date_shift"},
                    ],
                }
            ]
        }
        joined = "; ".join(_whole_column_state_rejections(cfg, table="t"))
        assert "when_predicate_not_chunk_stable" in joined
        assert "wa, wb" in joined  # sorted, comma-joined column list
        assert "date_shift_requires_explicit_format" in joined
        assert "da, db" in joined

    def test_nameless_offending_columns_render_question_mark(self):
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"strategy": "redact", "when": "x"},
                        {"strategy": "date_shift"},
                    ],
                }
            ]
        }
        joined = "; ".join(_whole_column_state_rejections(cfg, table="t"))
        assert "when_predicate_not_chunk_stable: column(s) ? " in joined
        assert "date_shift_requires_explicit_format: column(s) ? " in joined

    def test_date_shift_with_explicit_format_is_not_flagged(self):
        # Kills the mutations that invert/loosen the date_format lookup so a
        # PINNED-format date_shift column gets wrongly flagged.
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "d",
                            "strategy": "date_shift",
                            "provider_config": {"date_format": "%Y-%m-%d"},
                        }
                    ],
                }
            ]
        }
        assert _whole_column_state_rejections(cfg, table="t") == []


# --------------------------------------------------------------------------
# _runtime_source_rejections: presence / size / dtype / bucketize gates
# --------------------------------------------------------------------------


def _rr(tbl: pa.Table, **kw) -> list[str]:
    kw.setdefault("auto_chunk_threshold_rows", 0)
    return _runtime_source_rejections({"t": tbl}, table="t", **kw)


class TestRuntimeSourcePresenceAndSize:
    def test_extra_loaded_frames_are_rejected_and_named(self):
        s = pa.table({"x": pa.array(["a", "b"])})
        reasons = _runtime_source_rejections(
            {"t": s, "o1": s, "o2": s}, table="t", auto_chunk_threshold_rows=0
        )
        joined = "; ".join(reasons)
        assert "extra loaded source frame(s) o1, o2" in joined

    def test_missing_source_frame_is_rejected(self):
        joined = "; ".join(_runtime_source_rejections({}, table="t", auto_chunk_threshold_rows=0))
        assert "no loaded source frame for table 't'" in joined

    def test_lazy_source_is_declined_for_the_named_table(self, tmp_path):
        p = Path(tmp_path) / "t.parquet"
        pq.write_table(pa.table({"x": pa.array(["a", "b"])}), p)
        joined = "; ".join(
            _runtime_source_rejections(
                {"t": LazySource(path=p)}, table="t", auto_chunk_threshold_rows=0
            )
        )
        assert "source for table 't' is a lazy (LazySource) handle" in joined

    def test_below_threshold_rejected_at_boundary_not_at_equal(self):
        s = pa.table({"x": pa.array(["a", "b"])})  # 2 rows
        # Strictly below -> rejected.
        assert any(
            "below the auto-chunk threshold (3)" in r for r in _rr(s, auto_chunk_threshold_rows=3)
        )
        # Exactly at threshold -> admitted (kills the `<` -> `<=` boundary flip).
        assert _rr(s, auto_chunk_threshold_rows=2) == []


class TestRuntimeSourceDtypeGate:
    def test_integer_with_nulls_flagged_clean_integer_not(self):
        tbl = pa.table(
            {
                "int_nul": pa.array([1, None], type=pa.int64()),
                "int_clean": pa.array([1, 2], type=pa.int64()),
            }
        )
        joined = "; ".join(_rr(tbl))
        assert "int_nul (integer with nulls)" in joined
        assert "int_clean" not in joined  # kills `>= 0` and `> 1` null-count flips

    def test_stable_dtypes_are_not_flagged(self):
        assert _rr(pa.table({"f": pa.array([1.0, 2.0], type=pa.float64())})) == []
        assert _rr(pa.table({"b": pa.array([True, False], type=pa.bool_())})) == []
        assert _rr(pa.table({"ts": pa.array([datetime.datetime(2020, 1, 1)] * 2)})) == []
        assert _rr(pa.table({"n": pa.array([None, None], type=pa.null())})) == []
        assert _rr(pa.table({"s": pa.array(["a", "b"], type=pa.string())})) == []

    def test_exotic_dtype_flagged_with_type_and_sorted_join(self):
        tbl = pa.table(
            {
                "da": pa.array([1, 2], type=pa.decimal128(5, 2)),
                "db": pa.array([1, 2], type=pa.decimal128(5, 2)),
            }
        )
        joined = "; ".join(_rr(tbl))
        assert "column(s) with non-chunk-stable pandas round-trip dtypes:" in joined
        # Two columns, comma-joined and sorted (kills the separator + join-arg
        # mutations that would drop or corrupt the list).
        assert "da (decimal128(5, 2)), db (decimal128(5, 2))" in joined


class TestRuntimeSourceBucketizeGate:
    def test_clean_numeric_bucketize_source_is_admitted(self):
        # int64 no nulls: neither the dtype gate nor the bucketize gate fires.
        tbl = pa.table({"num": pa.array([1, 2], type=pa.int64())})
        assert _rr(tbl, bucketize_columns=["num"]) == []
        tbl_f = pa.table({"num": pa.array([1.0, 2.0], type=pa.float64())})
        assert _rr(tbl_f, bucketize_columns=["num"]) == []

    def test_non_numeric_bucketize_source_rejected(self):
        tbl = pa.table({"s": pa.array(["a", "b"], type=pa.string())})
        joined = "; ".join(_rr(tbl, bucketize_columns=["s"]))
        assert "bucketize_source_not_null_free_numeric" in joined  # lowercase code
        assert "s (string is not numeric)" in joined

    def test_numeric_bucketize_with_nulls_rejected(self):
        tbl = pa.table({"num": pa.array([1, None], type=pa.int64())})
        joined = "; ".join(_rr(tbl, bucketize_columns=["num"]))
        assert "num (numeric with nulls)" in joined  # kills `> 1` null-count flip

    def test_two_bad_bucketize_columns_are_comma_joined(self):
        tbl = pa.table({"sa": pa.array(["a", "b"]), "sb": pa.array(["a", "b"])})
        joined = "; ".join(_rr(tbl, bucketize_columns=["sa", "sb"]))
        assert "sa (string is not numeric), sb (string is not numeric)" in joined

    def test_unknown_bucketize_column_skipped_then_bad_one_still_flagged(self):
        # Kills `continue`->`break` (would abort before the real bad column) and
        # the `not in`->`in` schema-membership flip.
        tbl = pa.table({"zbad": pa.array(["x", "y"])})
        joined = "; ".join(_rr(tbl, bucketize_columns=["aa_unknown", "zbad"]))
        assert "zbad (string is not numeric)" in joined

    def test_empty_bucketize_list_yields_no_bucketize_reason(self):
        tbl = pa.table({"s": pa.array(["a", "b"])})
        assert _rr(tbl, bucketize_columns=[]) == []


# --------------------------------------------------------------------------
# _polars_native_rejection: substrate / mask-presence / fk / native-work gates
# --------------------------------------------------------------------------


def _scalar(strategy: str, table: str = "t"):
    return SimpleNamespace(kind="scalar", strategy=strategy, table=table)


class TestPolarsNativeRejection:
    def test_all_native_scalar_no_fk_on_polars_is_admitted(self):
        assert (
            _polars_native_rejection(
                substrate="polars", mask_tables=["t"], work=[_scalar("hash")], has_fk=False
            )
            is None
        )

    def test_non_native_scalar_strategy_is_named_and_rejects(self):
        reason = _polars_native_rejection(
            substrate="polars",
            mask_tables=["t"],
            work=[_scalar("zzz_not_native")],
            has_fk=False,
        )
        assert reason is not None  # kills the filter `and`->`or` (would admit)
        assert "non-polars-native work: zzz_not_native" in reason  # kills kind/strategy swap

    def test_multiple_non_native_strategies_are_comma_joined(self):
        reason = _polars_native_rejection(
            substrate="polars",
            mask_tables=["t"],
            work=[_scalar("aaa"), _scalar("bbb")],
            has_fk=False,
        )
        assert "non-polars-native work: aaa, bbb" in reason

    def test_fk_edges_reject_with_lowercase_code(self):
        reason = _polars_native_rejection(
            substrate="pandas", mask_tables=["t"], work=[_scalar("hash")], has_fk=True
        )
        assert "fk_resolution:" in reason  # kills the all-uppercase message mutation
        assert "resolved substrate is 'pandas'" in reason

    def test_no_mask_tables_rejects(self):
        reason = _polars_native_rejection(substrate="polars", mask_tables=[], work=[], has_fk=False)
        assert "no mask-kind work" in reason
