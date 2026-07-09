"""SC7a: bounded `profile_source` + `ProfileSource` readers acceptance tests.

Covers the consultant-2026-07-09 F1 acceptance sketch made concrete:
- `profile_source` on a Parquet source never reads the whole file (proved by
  monkeypatching the whole-file read paths to raise).
- `TableProfile.row_count` is the true footer count for Parquet, the O(1)
  `filesize // record_bytes` for fixed_width, and a flagged estimate for CSV.
- `row_count_exact` is True for parquet/fixed_width, False for csv.
- A source larger than the sample is flagged `sampled` with the true total
  row count and a bounded (never full) read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.profile import profile_source
from decoy_engine.profile._readers import (
    CsvFileSource,
    FixedWidthFileSource,
    ParquetFileSource,
    build_profile_source,
)


def _config(sources: dict[str, Any]) -> dict[str, Any]:
    return {"global_settings": {"seed": 0}, "sources": sources, "relationships": []}


def _write_parquet(path: Path, rows: int) -> pa.Table:
    table = pa.table(
        {
            "id": list(range(rows)),
            "label": [f"row-{i % 7}" for i in range(rows)],
        }
    )
    pq.write_table(table, path)
    return table


def _fixed_width_layout() -> dict[str, Any]:
    return {
        "columns": [
            {"name": "name", "start": 0, "width": 8, "type": "str"},
            {"name": "age", "start": 8, "width": 3, "type": "int"},
        ]
    }


# ---------------------------------------------------------------------
# Parquet: exact footer count, never a whole-file read
# ---------------------------------------------------------------------


class TestParquetBounded:
    def test_never_reads_whole_parquet_file(self, tmp_path: Path, monkeypatch) -> None:
        """The bounded path uses footer metadata + iter_batches only; the
        whole-file read paths (pq.read_table / pd.read_parquet) must never
        fire. Monkeypatch them to raise and prove profiling still succeeds."""
        path = tmp_path / "big.parquet"
        _write_parquet(path, rows=25_000)

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("whole-file parquet read must not happen on the bounded path")

        monkeypatch.setattr(pq, "read_table", _boom)
        monkeypatch.setattr(pd, "read_parquet", _boom)

        profile = profile_source(
            _config({"t": {"type": "file", "format": "parquet", "path": str(path)}}), seed=0
        )

        assert profile.tables[0].row_count == 25_000
        assert profile.tables[0].row_count_exact is True

    def test_row_count_is_exact_footer_count(self, tmp_path: Path) -> None:
        path = tmp_path / "small.parquet"
        _write_parquet(path, rows=42)
        reader = ParquetFileSource(path)
        rc = reader.row_count()
        assert rc.value == 42
        assert rc.exact is True

    def test_large_parquet_columns_flagged_sampled(self, tmp_path: Path) -> None:
        """A source larger than sample_rows keeps the true total row count but
        flags columns sampled (null/distinct come from the bounded sample)."""
        path = tmp_path / "big.parquet"
        _write_parquet(path, rows=25_000)
        profile = profile_source(
            _config({"t": {"type": "file", "format": "parquet", "path": str(path)}}),
            sample_rows=5_000,
            seed=0,
        )
        table = profile.tables[0]
        assert table.row_count == 25_000
        for col in table.columns:
            assert col.sampled is True
            assert col.row_count == 25_000
            # Invariant: sampled distinct/null never exceed the true total.
            assert col.null_count <= col.row_count
            if col.distinct_count is not None:
                assert col.distinct_count <= col.row_count
            # A bounded read can never claim a definitive candidate key.
            assert col.is_candidate_key_sampled is False

    def test_small_parquet_read_whole_is_not_sampled(self, tmp_path: Path) -> None:
        path = tmp_path / "small.parquet"
        _write_parquet(path, rows=30)
        profile = profile_source(
            _config({"t": {"type": "file", "format": "parquet", "path": str(path)}}),
            sample_rows=5_000,
            seed=0,
        )
        table = profile.tables[0]
        assert table.row_count == 30
        assert all(col.sampled is False for col in table.columns)


# ---------------------------------------------------------------------
# Fixed-width: O(1) exact count
# ---------------------------------------------------------------------


class TestFixedWidthBounded:
    def test_row_count_is_filesize_over_record_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "people.txt"
        path.write_text("alice   030\nbob     025\ncarol   041\n", encoding="utf-8")
        reader = FixedWidthFileSource(path, _fixed_width_layout())
        rc = reader.row_count()
        assert rc.value == 3
        assert rc.exact is True

    def test_profile_row_count_exact_true(self, tmp_path: Path) -> None:
        path = tmp_path / "people.txt"
        path.write_text("alice   030\nbob     025\ncarol   041\n", encoding="utf-8")
        profile = profile_source(
            _config(
                {
                    "t": {
                        "type": "file",
                        "format": "fixed_width",
                        "path": str(path),
                        "layout": _fixed_width_layout(),
                    }
                }
            ),
            seed=0,
        )
        assert profile.tables[0].row_count == 3
        assert profile.tables[0].row_count_exact is True

    def test_bounded_read_caps_records(self, tmp_path: Path) -> None:
        path = tmp_path / "people.txt"
        path.write_text(
            "".join(f"name{i:04d}{i % 100:03d}\n" for i in range(500)), encoding="utf-8"
        )
        reader = FixedWidthFileSource(path, _fixed_width_layout())
        sample = reader.sample_frame(10)
        assert len(sample) == 10
        assert reader.row_count().value == 500


# ---------------------------------------------------------------------
# CSV: flagged byte estimate
# ---------------------------------------------------------------------


class TestCsvBounded:
    def test_row_count_is_flagged_estimate(self, tmp_path: Path) -> None:
        path = tmp_path / "data.csv"
        rows = 5_000
        pd.DataFrame({"a": range(rows), "b": [f"v{i % 9}" for i in range(rows)]}).to_csv(
            path, index=False
        )
        reader = CsvFileSource(path)
        rc = reader.row_count()
        assert rc.exact is False
        # Coarse but in the right neighbourhood (uniform rows estimate well).
        assert 0.7 * rows <= rc.value <= 1.3 * rows

    def test_profile_row_count_exact_false(self, tmp_path: Path) -> None:
        path = tmp_path / "data.csv"
        path.write_text("email\na@x.com\nb@y.com\nc@z.com\n", encoding="utf-8")
        profile = profile_source(
            _config({"t": {"type": "file", "format": "csv", "path": str(path)}}), seed=0
        )
        table = profile.tables[0]
        assert table.row_count_exact is False
        # Estimate never falls below what the bounded sample actually read.
        assert table.row_count >= 3
        for col in table.columns:
            assert col.null_count <= col.row_count
            if col.distinct_count is not None:
                assert col.distinct_count <= col.row_count


# ---------------------------------------------------------------------
# residency + full-scan degrade
# ---------------------------------------------------------------------


class TestResidencyModes:
    def test_full_residency_reads_whole_and_is_exact(self, tmp_path: Path) -> None:
        path = tmp_path / "data.csv"
        pd.DataFrame({"a": range(120), "b": list("xy") * 60}).to_csv(path, index=False)
        profile = profile_source(
            _config({"t": {"type": "file", "format": "csv", "path": str(path)}}),
            sample_rows=None,
            residency="full",
            seed=0,
        )
        table = profile.tables[0]
        assert table.row_count == 120
        # A genuine whole read yields an exact count and non-sampled columns.
        assert table.row_count_exact is True
        assert all(col.sampled is False for col in table.columns)

    def test_bounded_full_scan_degrades_with_warning(self, tmp_path: Path) -> None:
        path = tmp_path / "big.parquet"
        _write_parquet(path, rows=25_000)
        with pytest.warns(UserWarning, match="Degrading to a bounded"):
            profile = profile_source(
                _config({"t": {"type": "file", "format": "parquet", "path": str(path)}}),
                sample_rows=None,
                residency="bounded",
                seed=0,
            )
        # Row count is still the true total; columns are sampled, not full-scanned.
        assert profile.tables[0].row_count == 25_000
        assert all(col.sampled for col in profile.tables[0].columns)

    def test_invalid_residency_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "small.parquet"
        _write_parquet(path, rows=5)
        with pytest.raises(ValueError, match="residency must be"):
            profile_source(
                _config({"t": {"type": "file", "format": "parquet", "path": str(path)}}),
                residency="streaming",
                seed=0,
            )


# ---------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------


class TestFactory:
    def test_builds_per_type(self, tmp_path: Path) -> None:
        path = tmp_path / "small.parquet"
        _write_parquet(path, rows=3)
        assert isinstance(
            build_profile_source({"type": "file", "format": "parquet", "path": str(path)}),
            ParquetFileSource,
        )

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="unsupported source type"):
            build_profile_source({"type": "sftp", "format": "csv"})
