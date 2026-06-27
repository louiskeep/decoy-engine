"""Unit tests for decoy_engine.reference_tables.

TDD: tests written before implementation. Covers:
  - loading each shipped table (row count + column schema)
  - loading a customer-provided table from a fixture path
  - version mismatch (wrong version logs warning + uses available version)
"""

from __future__ import annotations

import logging

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def _get_api():
    from decoy_engine.reference_tables import ReferenceTable, load_table

    return load_table, ReferenceTable


class TestShippedTables:
    def test_us_zip5_city_state_loads(self):
        load_table, ReferenceTable = _get_api()
        tbl = load_table("us_zip5_city_state")
        assert isinstance(tbl, ReferenceTable)

    def test_us_zip5_city_state_row_count(self):
        load_table, _ = _get_api()
        tbl = load_table("us_zip5_city_state")
        assert tbl.row_count >= 10, "shipped table must have at least 10 rows"

    def test_us_zip5_city_state_schema(self):
        load_table, _ = _get_api()
        tbl = load_table("us_zip5_city_state")
        assert "id" in tbl.column_names
        assert "zip" in tbl.column_names
        assert "city" in tbl.column_names
        assert "state" in tbl.column_names

    def test_us_zip5_city_state_has_population(self):
        """SP-08 forward: population column for HIPAA Safe Harbor (<20k check)."""
        load_table, _ = _get_api()
        tbl = load_table("us_zip5_city_state")
        assert "population" in tbl.column_names

    def test_vehicle_make_model_year_loads(self):
        load_table, ReferenceTable = _get_api()
        tbl = load_table("vehicle_make_model_year")
        assert isinstance(tbl, ReferenceTable)

    def test_vehicle_make_model_year_row_count(self):
        load_table, _ = _get_api()
        tbl = load_table("vehicle_make_model_year")
        assert tbl.row_count >= 10

    def test_vehicle_make_model_year_schema(self):
        load_table, _ = _get_api()
        tbl = load_table("vehicle_make_model_year")
        assert "id" in tbl.column_names
        assert "make" in tbl.column_names
        assert "model" in tbl.column_names
        assert "year" in tbl.column_names


class TestRandomAccess:
    def test_row_by_index(self):
        load_table, _ = _get_api()
        tbl = load_table("us_zip5_city_state")
        row = tbl.row(0)
        assert "id" in row
        assert "zip" in row

    def test_row_index_out_of_bounds(self):
        load_table, _ = _get_api()
        tbl = load_table("us_zip5_city_state")
        with pytest.raises((IndexError, KeyError, ValueError)):
            tbl.row(tbl.row_count + 9999)


class TestKeyedAccess:
    def test_keyed_access_returns_row(self):
        load_table, _ = _get_api()
        tbl = load_table("us_zip5_city_state")
        first_row = tbl.row(0)
        key_val = str(first_row["id"])
        row = tbl.keyed_row(key_val)
        assert "id" in row


class TestCustomerProvided:
    def test_load_customer_provided_table(self, tmp_path):
        """A customer drops a Parquet at a configured path; load_table reads it."""
        load_table, _ = _get_api()

        table = pa.table(
            {
                "id": pa.array([1, 2, 3], type=pa.int64()),
                "code": pa.array(["A", "B", "C"], type=pa.string()),
                "label": pa.array(["Alpha", "Beta", "Gamma"], type=pa.string()),
            }
        )
        parquet_path = tmp_path / "my_codes.parquet"
        pq.write_table(table, parquet_path)

        tbl = load_table("my_codes", path=parquet_path)
        assert tbl.row_count == 3
        assert "code" in tbl.column_names

    def test_customer_table_row_access(self, tmp_path):
        load_table, _ = _get_api()
        table = pa.table(
            {
                "id": pa.array([10, 20], type=pa.int64()),
                "value": pa.array(["X", "Y"], type=pa.string()),
            }
        )
        parquet_path = tmp_path / "vals.parquet"
        pq.write_table(table, parquet_path)

        tbl = load_table("vals", path=parquet_path)
        row = tbl.row(0)
        assert row["value"] == "X"


class TestVersionMismatch:
    def test_version_mismatch_logs_warning(self, tmp_path, caplog):
        """Loading a table with wrong version metadata logs a warning
        and still returns the table (graceful degradation).
        """
        load_table, _ = _get_api()

        table = pa.table(
            {
                "id": pa.array([1], type=pa.int64()),
                "zip": pa.array(["00001"], type=pa.string()),
                "city": pa.array(["Test City"], type=pa.string()),
                "state": pa.array(["TS"], type=pa.string()),
                "population": pa.array([1000], type=pa.int64()),
            },
            metadata={b"decoy_table_version": b"99.0"},
        )
        parquet_path = tmp_path / "us_zip5_city_state.parquet"
        pq.write_table(table, parquet_path)

        with caplog.at_level(logging.WARNING):
            tbl = load_table("us_zip5_city_state", path=parquet_path)

        assert any("version" in msg.lower() for msg in caplog.messages), (
            f"Expected a version-mismatch warning; got: {caplog.messages}"
        )
        assert tbl.row_count == 1

    def test_version_mismatch_still_returns_table(self, tmp_path):
        load_table, _ = _get_api()
        table = pa.table(
            {
                "id": pa.array([1, 2], type=pa.int64()),
                "zip": pa.array(["10001", "90210"], type=pa.string()),
                "city": pa.array(["New York", "Beverly Hills"], type=pa.string()),
                "state": pa.array(["NY", "CA"], type=pa.string()),
                "population": pa.array([1000000, 32000], type=pa.int64()),
            },
            metadata={b"decoy_table_version": b"0.0.1"},
        )
        parquet_path = tmp_path / "test_zip.parquet"
        pq.write_table(table, parquet_path)

        tbl = load_table("us_zip5_city_state", path=parquet_path)
        assert tbl is not None
