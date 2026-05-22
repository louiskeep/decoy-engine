"""Regression tests for the extension-dtype fix in StrategyManager.

Bug captured on 2026-05-22: a CSV column of 8-digit date integers
(20260522, 20260523, ...) gets read by the pandas/Arrow CSV path
as int64[pyarrow]. date_shift parses, shifts, and returns string-
typed dates. The original `result[column_name] = new_col`
assignment preserved the extension-dtype tag of the parent column,
so the now-string values got pushed through int64[pyarrow]'s
setitem path and raised the opaque

    ("object of type <class 'str'> cannot be converted to int",
     "Conversion failed for column ENRL_END_DT with type object")

Fix: when the original column is an extension dtype AND the
strategy returned a numpy-backed Series, drop and reinsert at the
same column index so the extension tag is shed and Arrow inference
on the way out (pa.Table.from_pandas) sees a clean object column
and emits it as string.
"""
from __future__ import annotations

import logging

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.transforms.registry import StrategyManager


@pytest.fixture
def logger():
    return logging.getLogger("test_mask_extension_dtype")


# ── Direct StrategyManager + Arrow round-trip ──────────────────────────────


class TestDateShiftOnIntColumn:
    """The original bug. int64[pyarrow] -> date_shift -> Arrow."""

    def test_int64_pyarrow_dates_become_string_dates(self, logger):
        df = pd.DataFrame({
            "ENRL_END_DT": pd.array(
                [20260522, 20260523, 20260524], dtype="int64[pyarrow]",
            ),
        })

        mgr = StrategyManager(seed=42, logger=logger)
        result = mgr.apply_masking_rules(df, [{
            "column": "ENRL_END_DT", "type": "date_shift",
            "min_days": -30, "max_days": 30, "date_format": "%Y%m%d",
        }])

        # Output dtype must be plain object (not int64[pyarrow]) so
        # downstream Arrow conversion infers string, not int.
        assert result["ENRL_END_DT"].dtype == object, (
            f"expected object dtype after date_shift; got {result['ENRL_END_DT'].dtype}"
        )
        # All values must be 8-digit date strings.
        for v in result["ENRL_END_DT"]:
            assert isinstance(v, str), f"expected str, got {type(v).__name__}"
            assert len(v) == 8 and v.isdigit(), (
                f"expected YYYYMMDD format; got {v!r}"
            )

    def test_arrow_round_trip_succeeds(self, logger):
        """The bug surfaced when pa.Table.from_pandas tried to honor
        the parent column's int64 tag at the op boundary
        (graph/conversion.py engine_to_arrow). Reproduces that path."""
        df = pd.DataFrame({
            "ENRL_END_DT": pd.array(
                [20260522, 20260523, 20260524], dtype="int64[pyarrow]",
            ),
            "ID": pd.array([1, 2, 3], dtype="int64[pyarrow]"),
        })

        mgr = StrategyManager(seed=42, logger=logger)
        result = mgr.apply_masking_rules(df, [{
            "column": "ENRL_END_DT", "type": "date_shift",
            "min_days": -30, "max_days": 30, "date_format": "%Y%m%d",
        }])

        # The pa.Table.from_pandas call is what blew up at runtime.
        table = pa.Table.from_pandas(result, preserve_index=False)
        # Masked column lands as string; untouched int column stays int64.
        assert pa.types.is_string(table.schema.field("ENRL_END_DT").type)
        assert pa.types.is_integer(table.schema.field("ID").type)

    def test_column_order_preserved_after_drop_reinsert(self, logger):
        """The fix uses drop + insert(col_idx) so the masked column
        ends up at its original position, not appended. Verify the
        rest of the frame's column order is intact."""
        df = pd.DataFrame({
            "before_col": pd.array([1, 2, 3], dtype="int64[pyarrow]"),
            "ENRL_END_DT": pd.array(
                [20260522, 20260523, 20260524], dtype="int64[pyarrow]",
            ),
            "after_col": pd.array([10, 20, 30], dtype="int64[pyarrow]"),
        })

        mgr = StrategyManager(seed=42, logger=logger)
        result = mgr.apply_masking_rules(df, [{
            "column": "ENRL_END_DT", "type": "date_shift",
            "min_days": -30, "max_days": 30, "date_format": "%Y%m%d",
        }])

        assert list(result.columns) == ["before_col", "ENRL_END_DT", "after_col"], (
            f"column order changed; got {list(result.columns)}"
        )


class TestOtherStrategiesOnIntColumn:
    """date_shift wasn't the only victim. Hash and faker also write
    string outputs into columns the reader may have inferred as
    int. Same fix protects them."""

    def test_hash_on_int64_pyarrow_column(self, logger):
        df = pd.DataFrame({
            "SSN": pd.array([123456789, 987654321], dtype="int64[pyarrow]"),
        })

        mgr = StrategyManager(seed=42, logger=logger)
        result = mgr.apply_masking_rules(df, [{
            "column": "SSN", "type": "hash",
        }])

        # Hash output is hex string; Arrow round-trip must work.
        assert result["SSN"].dtype == object
        table = pa.Table.from_pandas(result, preserve_index=False)
        assert pa.types.is_string(table.schema.field("SSN").type)

    def test_redact_on_int64_pyarrow_column(self, logger):
        df = pd.DataFrame({
            "ACCOUNT_ID": pd.array([1001, 1002, 1003], dtype="int64[pyarrow]"),
        })

        mgr = StrategyManager(seed=42, logger=logger)
        result = mgr.apply_masking_rules(df, [{
            "column": "ACCOUNT_ID", "type": "redact",
        }])

        table = pa.Table.from_pandas(result, preserve_index=False)
        assert pa.types.is_string(table.schema.field("ACCOUNT_ID").type)


class TestNonExtensionDtypePathUnchanged:
    """The fix only kicks in when the original column is an
    extension dtype. Plain numpy-backed columns (the historical
    default) must still flow through the legacy assignment path."""

    def test_object_dtype_input_object_dtype_output(self, logger):
        df = pd.DataFrame({
            "name": pd.Series(["Alice", "Bob"], dtype=object),
        })

        mgr = StrategyManager(seed=42, logger=logger)
        result = mgr.apply_masking_rules(df, [{
            "column": "name", "type": "redact",
        }])

        assert result["name"].dtype == object
        table = pa.Table.from_pandas(result, preserve_index=False)
        assert pa.types.is_string(table.schema.field("name").type)

    def test_passthrough_on_int64_pyarrow_preserves_extension_dtype(self, logger):
        """Passthrough doesn't change the type semantically, so the
        extension-dtype tag should be preserved -- the fix only
        kicks in when the new column's dtype is NOT an extension
        dtype (i.e. when the strategy actually changed the type)."""
        df = pd.DataFrame({
            "ID": pd.array([1, 2, 3], dtype="int64[pyarrow]"),
        })

        mgr = StrategyManager(seed=42, logger=logger)
        result = mgr.apply_masking_rules(df, [{
            "column": "ID", "type": "passthrough",
        }])

        # Passthrough returns the original Series unchanged -- dtype
        # should stay int64[pyarrow] so Arrow emits int64.
        assert str(result["ID"].dtype) == "int64[pyarrow]"


class TestMultipleColumnsOneMasked:
    """The fix processes columns one at a time. Verify that masking
    column A doesn't perturb the dtype of column B."""

    def test_unrelated_column_keeps_its_extension_dtype(self, logger):
        df = pd.DataFrame({
            "DATE_INT": pd.array(
                [20260522, 20260523], dtype="int64[pyarrow]",
            ),
            "AMOUNT": pd.array(
                [100.50, 200.75], dtype="float64[pyarrow]",
            ),
        })

        mgr = StrategyManager(seed=42, logger=logger)
        result = mgr.apply_masking_rules(df, [{
            "column": "DATE_INT", "type": "date_shift",
            "min_days": -30, "max_days": 30, "date_format": "%Y%m%d",
        }])

        # DATE_INT was masked; dtype shed.
        assert result["DATE_INT"].dtype == object
        # AMOUNT was untouched; keeps the original extension dtype.
        assert str(result["AMOUNT"].dtype) == "double[pyarrow]"
