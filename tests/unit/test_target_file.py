"""target_file op contract tests — local write path.

Tests focus on the pandas engine path (``__engine: pandas``) because the
DuckDB path requires the full DuckDB library and is exercised by the
integration test suite. The contract assertions are engine-agnostic: both
paths must write a byte-for-byte correct file and return the documented
shape.

Coverage targets (from Sprint 4 Done-when criterion):
  - CSV write round-trip
  - Parquet write round-trip
  - Format inference from extension (no explicit ``format`` key)
  - Directory auto-creation (parents=True)
  - validate_config: missing output_filename
  - validate_config: unsupported format string
  - Preview mode: returns input DataFrame without touching the filesystem
  - ctx.export: rows_written, output_path, output_file_size_bytes emitted
"""
from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.graph.ops import target_file
from decoy_engine.internal.validator import ValidationError


# ----- helpers -----------------------------------------------------------


def _simple_df(rows: int = 5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": list(range(1, rows + 1)),
            "name": [f"person_{i}" for i in range(1, rows + 1)],
            "score": [float(i) * 1.5 for i in range(1, rows + 1)],
        }
    )


class _RecordingCtx:
    """Minimal ctx that records ctx.export calls."""

    def __init__(self):
        self._exports: dict[str, object] = {}

    def export(self, key: str, value: object) -> None:
        self._exports[key] = value


# ----- validate_config ---------------------------------------------------


class TestValidateConfig:
    def test_missing_output_filename_raises(self):
        with pytest.raises(ValidationError, match="output_filename"):
            target_file.validate_config({})

    def test_unsupported_format_raises(self):
        with pytest.raises(ValidationError, match="unsupported format"):
            target_file.validate_config(
                {"output_filename": "out.csv", "format": "xlsx"}
            )

    def test_csv_extension_inferred_is_valid(self):
        target_file.validate_config({"output_filename": "out.csv"})

    def test_parquet_extension_inferred_is_valid(self):
        target_file.validate_config({"output_filename": "out.parquet"})

    def test_pq_extension_inferred_as_parquet(self):
        target_file.validate_config({"output_filename": "out.pq"})

    def test_explicit_csv_format_is_valid(self):
        target_file.validate_config(
            {"output_filename": "out.bin", "format": "csv"}
        )

    def test_explicit_parquet_format_is_valid(self):
        target_file.validate_config(
            {"output_filename": "out.bin", "format": "parquet"}
        )


# ----- CSV round-trip ----------------------------------------------------


class TestCSVWrite:
    def test_csv_round_trip(self, tmp_path):
        df = _simple_df()
        out = tmp_path / "output.csv"
        config = {"output_filename": str(out), "__engine": "pandas"}
        result = target_file.apply([df], config, ctx=None)
        # Contract: written file exists and can be read back
        assert out.exists(), "CSV file was not created"
        read_back = pd.read_csv(out)
        assert list(read_back.columns) == list(df.columns)
        assert len(read_back) == len(df)
        assert list(read_back["id"]) == list(df["id"])

    def test_csv_format_inferred_from_extension(self, tmp_path):
        df = _simple_df(3)
        out = tmp_path / "data.csv"
        # No `format` key in config — must infer from extension.
        target_file.apply([df], {"output_filename": str(out), "__engine": "pandas"}, ctx=None)
        assert out.exists()
        assert len(pd.read_csv(out)) == 3

    def test_csv_apply_returns_empty_df_with_correct_schema(self, tmp_path):
        df = _simple_df(4)
        out = tmp_path / "schema_check.csv"
        result = target_file.apply([df], {"output_filename": str(out), "__engine": "pandas"}, ctx=None)
        # Sink ops return an empty value; for the pandas path that's a 0-row df.
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
        assert list(result.columns) == list(df.columns)

    def test_csv_preserves_unicode(self, tmp_path):
        df = pd.DataFrame({"name": ["Ångström", "Müller", "日本語"]})
        out = tmp_path / "unicode.csv"
        target_file.apply([df], {"output_filename": str(out), "__engine": "pandas"}, ctx=None)
        read_back = pd.read_csv(out, encoding="utf-8")
        assert list(read_back["name"]) == list(df["name"])


# ----- Parquet round-trip ------------------------------------------------


class TestParquetWrite:
    def test_parquet_round_trip(self, tmp_path):
        df = _simple_df()
        out = tmp_path / "output.parquet"
        config = {"output_filename": str(out), "__engine": "pandas"}
        target_file.apply([df], config, ctx=None)
        assert out.exists()
        read_back = pd.read_parquet(out)
        assert list(read_back.columns) == list(df.columns)
        assert len(read_back) == len(df)

    def test_pq_extension_writes_parquet(self, tmp_path):
        df = _simple_df(2)
        out = tmp_path / "data.pq"
        target_file.apply([df], {"output_filename": str(out), "__engine": "pandas"}, ctx=None)
        assert out.exists()
        read_back = pd.read_parquet(out)
        assert len(read_back) == 2

    def test_explicit_parquet_format_overrides_extension(self, tmp_path):
        df = _simple_df(3)
        out = tmp_path / "data.bin"  # ambiguous extension
        target_file.apply(
            [df],
            {"output_filename": str(out), "format": "parquet", "__engine": "pandas"},
            ctx=None,
        )
        # Must be readable as parquet despite the .bin extension.
        read_back = pd.read_parquet(out)
        assert len(read_back) == 3


# ----- Directory auto-creation -------------------------------------------


class TestDirectoryCreation:
    def test_creates_missing_parent_directories(self, tmp_path):
        out = tmp_path / "a" / "b" / "c" / "out.csv"
        df = _simple_df(2)
        target_file.apply([df], {"output_filename": str(out), "__engine": "pandas"}, ctx=None)
        assert out.exists()


# ----- Preview mode -------------------------------------------------------


class TestPreviewMode:
    def test_preview_returns_df_without_writing(self, tmp_path):
        df = _simple_df(5)
        out = tmp_path / "preview_should_not_exist.csv"
        result = target_file.apply(
            [df],
            {
                "output_filename": str(out),
                "__engine": "pandas",
                "__preview_row_limit": 3,
            },
            ctx=None,
        )
        # Preview returns the input DataFrame without writing the file.
        assert not out.exists(), "Preview mode must not write the file"
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(df)


# ----- ctx.export ---------------------------------------------------------


class TestCtxExport:
    def test_rows_written_exported(self, tmp_path):
        df = _simple_df(7)
        out = tmp_path / "exported.csv"
        ctx = _RecordingCtx()
        target_file.apply([df], {"output_filename": str(out), "__engine": "pandas"}, ctx=ctx)
        assert ctx._exports["rows_written"] == 7

    def test_output_path_exported(self, tmp_path):
        df = _simple_df(2)
        out = tmp_path / "path_export.csv"
        ctx = _RecordingCtx()
        target_file.apply([df], {"output_filename": str(out), "__engine": "pandas"}, ctx=ctx)
        exported = ctx._exports["output_path"]
        # Must be an absolute path string pointing to the file we wrote.
        assert isinstance(exported, str)
        assert exported.endswith("path_export.csv")

    def test_output_file_size_bytes_exported(self, tmp_path):
        df = _simple_df(10)
        out = tmp_path / "sized.csv"
        ctx = _RecordingCtx()
        target_file.apply([df], {"output_filename": str(out), "__engine": "pandas"}, ctx=ctx)
        size = ctx._exports.get("output_file_size_bytes")
        # Size must be a positive integer matching the actual on-disk file.
        assert isinstance(size, int)
        assert size > 0
        assert size == out.stat().st_size

    def test_no_ctx_does_not_raise(self, tmp_path):
        df = _simple_df(3)
        out = tmp_path / "no_ctx.csv"
        # Passing ctx=None must not raise.
        target_file.apply([df], {"output_filename": str(out), "__engine": "pandas"}, ctx=None)
        assert out.exists()
