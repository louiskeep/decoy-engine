"""Tests for the convert.file_type graph op (Items 57 + 66(b)).

Covers config validation, per-format round-trips (CSV / TSV / Parquet /
JSONL), stream passthrough semantics, preview-mode bypass of the write,
parent-directory creation, error mapping, and OPS registry plumbing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.graph.ops import OPS, convert_file_type
from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError


def _table() -> pa.Table:
    return pa.table({"id": [1, 2, 3], "name": ["alice", "bob", "carol"]})


class TestValidation:
    def test_missing_format_rejected(self):
        with pytest.raises(ValidationError, match="format"):
            convert_file_type.validate_config({"output_filename": "out.csv"})

    def test_empty_format_rejected(self):
        with pytest.raises(ValidationError, match="format"):
            convert_file_type.validate_config({"format": "", "output_filename": "out.csv"})

    def test_non_string_format_rejected(self):
        with pytest.raises(ValidationError, match="format"):
            convert_file_type.validate_config({"format": 42, "output_filename": "out.csv"})

    def test_unsupported_format_rejected(self):
        with pytest.raises(ValidationError, match="xml"):
            convert_file_type.validate_config({"format": "xml", "output_filename": "out.xml"})

    def test_missing_output_filename_rejected(self):
        with pytest.raises(ValidationError, match="output_filename"):
            convert_file_type.validate_config({"format": "csv"})

    @pytest.mark.parametrize("fmt", ["csv", "tsv", "parquet", "jsonl"])
    def test_supported_formats_pass(self, fmt):
        convert_file_type.validate_config({"format": fmt, "output_filename": f"out.{fmt}"})

    def test_format_is_case_insensitive(self):
        convert_file_type.validate_config({"format": "Parquet", "output_filename": "out.parquet"})


class TestApplyRoundTrip:
    def test_csv_write_and_passthrough(self, tmp_path: Path):
        out = tmp_path / "rows.csv"
        result = convert_file_type.apply(
            inputs=[_table()],
            config={"format": "csv", "output_filename": str(out)},
            ctx=None,
        )
        assert out.exists()
        text = out.read_text()
        assert text.splitlines()[0] == "id,name"
        assert "alice" in text and "bob" in text and "carol" in text
        # stream semantics: input flows through unchanged
        assert isinstance(result, pa.Table)
        assert result.num_rows == 3
        assert result.column_names == ["id", "name"]

    def test_tsv_uses_tab_delimiter(self, tmp_path: Path):
        out = tmp_path / "rows.tsv"
        convert_file_type.apply(
            inputs=[_table()],
            config={"format": "tsv", "output_filename": str(out)},
            ctx=None,
        )
        first_line = out.read_text().splitlines()[0]
        assert first_line == "id\tname"

    def test_parquet_round_trip_preserves_types(self, tmp_path: Path):
        out = tmp_path / "rows.parquet"
        convert_file_type.apply(
            inputs=[_table()],
            config={"format": "parquet", "output_filename": str(out)},
            ctx=None,
        )
        import pyarrow.parquet as pq

        back = pq.read_table(out)
        assert back.num_rows == 3
        assert back.column("id").to_pylist() == [1, 2, 3]
        assert back.column("name").to_pylist() == ["alice", "bob", "carol"]

    def test_jsonl_writes_one_object_per_line(self, tmp_path: Path):
        out = tmp_path / "rows.jsonl"
        convert_file_type.apply(
            inputs=[_table()],
            config={"format": "jsonl", "output_filename": str(out)},
            ctx=None,
        )
        lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
        assert len(lines) == 3
        rows = [json.loads(ln) for ln in lines]
        assert rows[0] == {"id": 1, "name": "alice"}
        assert rows[2] == {"id": 3, "name": "carol"}

    def test_uppercase_format_still_works(self, tmp_path: Path):
        out = tmp_path / "rows.csv"
        convert_file_type.apply(
            inputs=[_table()],
            config={"format": "CSV", "output_filename": str(out)},
            ctx=None,
        )
        assert out.exists()
        assert out.read_text().splitlines()[0] == "id,name"


class TestSideEffects:
    def test_creates_parent_directory(self, tmp_path: Path):
        out = tmp_path / "nested" / "deeper" / "rows.csv"
        convert_file_type.apply(
            inputs=[_table()],
            config={"format": "csv", "output_filename": str(out)},
            ctx=None,
        )
        assert out.exists()

    def test_preview_mode_skips_write(self, tmp_path: Path):
        out = tmp_path / "preview.csv"
        result = convert_file_type.apply(
            inputs=[_table()],
            config={
                "format": "csv",
                "output_filename": str(out),
                "__preview_row_limit": 10,
            },
            ctx=None,
        )
        assert not out.exists()
        # passthrough still returns the input table for downstream preview
        assert isinstance(result, pa.Table)
        assert result.num_rows == 3


class TestErrors:
    def test_unwritable_path_raises_op_error(self, tmp_path: Path):
        # output_filename points at an existing directory, so DuckDB's
        # COPY ... TO can't write through it; we expect that failure to
        # surface as OpError, not the raw duckdb exception.
        out = tmp_path / "existing_dir"
        out.mkdir()
        with pytest.raises(OpError, match=r"convert\.file_type"):
            convert_file_type.apply(
                inputs=[_table()],
                config={"format": "csv", "output_filename": str(out)},
                ctx=None,
            )


class TestRegistry:
    def test_registered_in_ops_dispatch(self):
        assert "convert.file_type" in OPS
        op = OPS["convert.file_type"]
        assert op.KIND == "convert.file_type"
        assert op.NATIVE_ENGINE == "duckdb"
        assert op.INPUT_ARITY == (1, 1)
        assert op.OUTPUT_KIND == "stream"
