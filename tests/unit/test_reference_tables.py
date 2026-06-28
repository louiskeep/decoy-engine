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


class TestKeyedAccessOrderIndependence:
    """H1: keyed_row must produce id-sorted positional access.

    The HMAC-keyed index is applied over rows sorted by the id column,
    not over rows in Parquet file order. This makes keyed access
    deterministic regardless of how the Parquet was written.
    """

    def test_keyed_row_deterministic_within_version(self, tmp_path):
        """Same key always maps to same row in the same table."""
        load_table, _ = _get_api()
        tbl = load_table("us_zip5_city_state")
        row_a = tbl.keyed_row("primary_key_0001")
        row_b = tbl.keyed_row("primary_key_0001")
        assert row_a == row_b

    def test_keyed_row_independent_of_parquet_row_order(self, tmp_path):
        """Two tables with the same rows in different Parquet order must
        return the same row for the same key after the id-sort fix."""
        import pyarrow as pa

        from decoy_engine.reference_tables._types import ReferenceTable

        # id-ascending order
        table_asc = pa.table(
            {
                "id": pa.array([1, 2, 3], type=pa.int64()),
                "label": pa.array(["Alpha", "Beta", "Gamma"], type=pa.string()),
            }
        )
        # Same data, descending Parquet row order.
        table_desc = pa.table(
            {
                "id": pa.array([3, 2, 1], type=pa.int64()),
                "label": pa.array(["Gamma", "Beta", "Alpha"], type=pa.string()),
            }
        )

        tbl_asc = ReferenceTable(table_asc, "test_asc")
        tbl_desc = ReferenceTable(table_desc, "test_desc")

        for key in ["alice", "bob", "charlie", "999", "hello_world"]:
            row_asc = tbl_asc.keyed_row(key)
            row_desc = tbl_desc.keyed_row(key)
            assert row_asc == row_desc, (
                f"key={key!r}: asc returned {row_asc}, desc returned {row_desc}. "
                f"keyed_row must be id-sorted-positional, not Parquet-order."
            )

    def test_keyed_row_returns_correct_id_sorted_row(self, tmp_path):
        """Verify that keyed_row selects from id-sorted rows.

        With 2 rows (id=10, id=20) and a key that maps to index 0 via
        HMAC%2, the result must be the row with the LOWER id (id=10)
        regardless of which order the rows appear in the Parquet file.
        """
        import pyarrow as pa

        from decoy_engine.internal.crypto import hmac_hex
        from decoy_engine.reference_tables._types import ReferenceTable

        _SALT = b"decoy.reference_tables.keyed_access.v1"

        # Identify a key that maps to index 0 (mod 2).
        key = None
        for candidate in ["a", "b", "c", "d", "e", "f", "g", "h"]:
            hex_d = hmac_hex(_SALT, candidate)
            if hex_d is not None and int(hex_d[:8], 16) % 2 == 0:
                key = candidate
                break
        assert key is not None, "could not find a key that maps to index 0 mod 2"

        # Table written with id=20 first, id=10 second.
        table_inverted = pa.table(
            {
                "id": pa.array([20, 10], type=pa.int64()),
                "label": pa.array(["High", "Low"], type=pa.string()),
            }
        )
        tbl = ReferenceTable(table_inverted, "test_inverted")
        row = tbl.keyed_row(key)
        # After id-sort, index 0 = row with id=10 (label="Low").
        assert row["label"] == "Low", (
            f"Expected id-sorted row 0 (id=10, label='Low') but got {row}. "
            f"keyed_row must sort by id before applying modular index."
        )


class TestIdTypeEnforcement:
    """M3: the 'id' column must be int64; a string 'id' is rejected."""

    def test_string_id_column_raises_value_error(self, tmp_path):
        """A customer Parquet with string 'id' must be rejected at load."""
        load_table, _ = _get_api()

        table = pa.table(
            {
                "id": pa.array(["row_a", "row_b"], type=pa.string()),
                "code": pa.array(["X", "Y"], type=pa.string()),
            }
        )
        parquet_path = tmp_path / "bad_id_type.parquet"
        pq.write_table(table, parquet_path)

        with pytest.raises(ValueError, match="int64"):
            load_table("bad_id_type", path=parquet_path)

    def test_int32_id_column_raises_value_error(self, tmp_path):
        """int32 id is also rejected -- convention specifies int64."""
        load_table, _ = _get_api()

        table = pa.table(
            {
                "id": pa.array([1, 2], type=pa.int32()),
                "code": pa.array(["X", "Y"], type=pa.string()),
            }
        )
        parquet_path = tmp_path / "int32_id.parquet"
        pq.write_table(table, parquet_path)

        with pytest.raises(ValueError, match="int64"):
            load_table("int32_id", path=parquet_path)

    def test_valid_int64_id_loads_correctly(self, tmp_path):
        """A proper int64 id column passes validation without error."""
        load_table, _ = _get_api()

        table = pa.table(
            {
                "id": pa.array([1, 2, 3], type=pa.int64()),
                "value": pa.array(["A", "B", "C"], type=pa.string()),
            }
        )
        parquet_path = tmp_path / "good_id.parquet"
        pq.write_table(table, parquet_path)

        tbl = load_table("good_id", path=parquet_path)
        assert tbl.row_count == 3


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
