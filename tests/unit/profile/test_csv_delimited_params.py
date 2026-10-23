"""Profiler honours FileSource delimited params (has_header/column_names/delimiter).

The engine profiler (scan / STORM) and the CLI mask reader consume the same
FileSource fields. This guards that profiling a headerless file under supplied
names yields the same frame as the equivalent headered file, and that a
non-comma delimiter is respected.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from decoy_engine.errors import PipelineValidationError
from decoy_engine.profile._readers import CsvFileSource, build_profile_source
from decoy_engine.validation_result import CODES

_ROWS = "1,Ada,ada@example.com\n2,Grace,grace@example.com\n"
_NAMES = ["id", "name", "email"]


def test_headerless_with_names_matches_headered(tmp_path: Path) -> None:
    headered = tmp_path / "with_header.csv"
    headered.write_text("id,name,email\n" + _ROWS, encoding="utf-8")
    headerless = tmp_path / "no_header.csv"
    headerless.write_text(_ROWS, encoding="utf-8")

    a = CsvFileSource(headered).to_frame()
    b = CsvFileSource(headerless, has_header=False, column_names=_NAMES).to_frame()

    pd.testing.assert_frame_equal(a, b)
    assert list(b.columns) == _NAMES


def test_headerless_schema_uses_supplied_names(tmp_path: Path) -> None:
    path = tmp_path / "no_header.csv"
    path.write_text(_ROWS, encoding="utf-8")
    schema = CsvFileSource(path, has_header=False, column_names=_NAMES).schema()
    assert schema.names == _NAMES


def test_tab_delimiter_is_respected(tmp_path: Path) -> None:
    path = tmp_path / "data.tsv"
    path.write_text("id\tname\n1\tAda\n2\tGrace\n", encoding="utf-8")
    frame = CsvFileSource(path, delimiter="\t").to_frame()
    assert list(frame.columns) == ["id", "name"]
    assert frame["name"].tolist() == ["Ada", "Grace"]


def test_undercount_names_raises_not_silent_shift(tmp_path: Path) -> None:
    # pandas with a too-short `names=` silently pushes the first column into the
    # index and mislabels the rest; the per-record width scan rejects instead.
    path = tmp_path / "no_header.csv"
    path.write_text(_ROWS, encoding="utf-8")
    with pytest.raises(PipelineValidationError, match="line 1 has 3 fields") as exc:
        CsvFileSource(path, has_header=False, column_names=["id"]).to_frame()
    assert exc.value.code == CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH


def test_overcount_names_raises_not_nan_pad(tmp_path: Path) -> None:
    path = tmp_path / "no_header.csv"
    path.write_text(_ROWS, encoding="utf-8")
    with pytest.raises(PipelineValidationError, match="declares 4"):
        CsvFileSource(
            path, has_header=False, column_names=["id", "name", "email", "extra"]
        ).to_frame()


def test_ragged_short_row_fails_closed(tmp_path: Path) -> None:
    # A single short row among correct rows: pandas would pad/shift and keep the
    # frame width, hiding the misalignment. The per-record scan catches the row.
    path = tmp_path / "ragged.csv"
    path.write_text("1,Ada,ada@example.com\n2,grace@example.com\n", encoding="utf-8")
    with pytest.raises(PipelineValidationError, match="line 2 has 2 fields") as exc:
        CsvFileSource(path, has_header=False, column_names=_NAMES).to_frame()
    assert exc.value.code == CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH


def test_overwide_row_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "wide.csv"
    path.write_text("1,Ada\n2,Grace,extra\n", encoding="utf-8")
    with pytest.raises(PipelineValidationError, match="line 2 has 3 fields"):
        CsvFileSource(path, has_header=False, column_names=["id", "name"]).to_frame()


def test_empty_headerless_file_is_coded_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(PipelineValidationError, match="no data rows") as exc:
        CsvFileSource(path, has_header=False, column_names=["id"]).to_frame()
    assert exc.value.code == CODES.SOURCE_FILE_COLUMN_COUNT_MISMATCH


def test_quoted_delimiter_counts_as_one_field(tmp_path: Path) -> None:
    # A delimiter inside a quoted field is one field, not two; the csv-module
    # scan must agree with pandas so a valid quoted file is not falsely rejected.
    path = tmp_path / "quoted.csv"
    path.write_text('1,"Ada, Jr.",ada@example.com\n', encoding="utf-8")
    frame = CsvFileSource(path, has_header=False, column_names=_NAMES).to_frame()
    assert frame.iloc[0]["name"] == "Ada, Jr."


@pytest.mark.parametrize(
    "label,content",
    [
        ("bom_quoted", b'\xef\xbb\xbf1,"a,b",c@x\n2,d,e@x\n'),
        ("ws_only_line", b"1,Ada,a@x\n   \n2,Bob,b@x\n"),
        ("trailing_spaced_line", b"1,Ada,a@x\n2,Bob,b@x\n  \n"),
        ("ws_with_delim", b"1,Ada,a@x\n , , \n2,Bob,b@x\n"),
        ("large_field", b"1," + b"x" * 200000 + b",a@x\n"),
        ("multiline_quoted", b'1,"Ada\nJr",a@x\n2,Bob,b@x\n'),
        ("crlf", b"1,Ada,a@x\r\n2,Bob,b@x\r\n"),
    ],
)
def test_valid_file_is_not_falsely_rejected(tmp_path: Path, label: str, content: bytes) -> None:
    # The width scan must agree with pandas: none of these valid shapes (BOM,
    # whitespace-only / trailing-spaced lines, a whitespace-field row, a large
    # field, a multiline-quoted field, CRLF) may be rejected.
    path = tmp_path / f"{label}.csv"
    path.write_bytes(content)
    frame = CsvFileSource(path, has_header=False, column_names=_NAMES).to_frame()
    assert list(frame.columns) == _NAMES


@pytest.mark.parametrize(
    "label,content",
    [
        # NUL in a plain field, NUL before a quote (the r3 tokenization
        # disagreement), and NUL opening a multiline quote (the r4 case where a
        # scan-skipped record would otherwise surface a non-blank pandas cell).
        ("nul_in_field", b"1,Ad\x00a,a@x\n2,Bob,b@x\n"),
        ("nul_before_quote", b'\x00"a,b",c,d\n'),
        ("nul_opens_multiline_quote", b'1,2,3\n\x00"\n "\n4,5,6\n'),
    ],
)
def test_nul_bearing_file_is_rejected_coded(tmp_path: Path, label: str, content: bytes) -> None:
    # NUL is not reconcilable between the csv scan and pandas, so a NUL-bearing file
    # fails closed with the coded `source_file.nul_byte` before pandas tokenizes it.
    # This is what keeps the acceptance claim below sound. Message carries no cell value.
    path = tmp_path / f"{label}.csv"
    path.write_bytes(content)
    with pytest.raises(PipelineValidationError) as exc:
        CsvFileSource(path, has_header=False, column_names=_NAMES).to_frame()
    assert exc.value.code == CODES.SOURCE_FILE_NUL_BYTE
    assert "a,b" not in str(exc.value)
    assert "Ad" not in str(exc.value)


def test_blank_interior_line_is_documented_limitation(tmp_path: Path) -> None:
    # Documented limitation (see CsvFileSource._assert_record_widths): a line whose
    # sole field is quoted-empty is treated as blank by the width scan, while pandas
    # retains it and pads to an all-empty row. With NUL-bearing files rejected, the
    # scan and pandas tokenize every admitted file identically, so a skipped line is
    # genuinely blank to pandas too and pads to an all-empty row (never a non-blank
    # cell, never a value misalignment). Pin the behavior so any change is deliberate.
    path = tmp_path / "blank_interior.csv"
    path.write_bytes(b'1,2,3\n""\n4,5,6\n')
    frame = CsvFileSource(path, has_header=False, column_names=_NAMES).to_frame()
    assert frame.shape == (3, 3)
    middle = frame.iloc[1]
    assert all((v == "") or pd.isna(v) for v in middle), list(middle)


def test_mismatch_line_number_is_physical(tmp_path: Path) -> None:
    # A multiline-quoted first record spans two physical lines, so the mismatch
    # on the next record is reported at its physical line, not its record ordinal.
    path = tmp_path / "multiline_then_bad.csv"
    path.write_bytes(b'1,"Ada\nJr",a@x\n2,b@x\n')
    with pytest.raises(PipelineValidationError, match="line 3 has 2 fields"):
        CsvFileSource(path, has_header=False, column_names=_NAMES).to_frame()


def test_error_message_never_embeds_a_cell_value(tmp_path: Path) -> None:
    # Leak rule: the mismatch message names path/line/counts, never a cell value.
    path = tmp_path / "ragged.csv"
    path.write_text("1,secretvalue\n", encoding="utf-8")
    with pytest.raises(PipelineValidationError) as exc:
        CsvFileSource(path, has_header=False, column_names=_NAMES).to_frame()
    assert "secretvalue" not in str(exc.value)


def test_headerless_row_count_counts_first_data_row(tmp_path: Path) -> None:
    # The headerless branch skips no header line, so every data row is counted.
    path = tmp_path / "no_header.csv"
    path.write_text(_ROWS, encoding="utf-8")  # two data rows
    rc = CsvFileSource(path, has_header=False, column_names=_NAMES).row_count()
    assert rc.exact is False
    assert rc.value >= 2


def test_build_profile_source_threads_params(tmp_path: Path) -> None:
    path = tmp_path / "no_header.csv"
    path.write_text(_ROWS, encoding="utf-8")
    reader = build_profile_source(
        {
            "type": "file",
            "format": "csv",
            "path": str(path),
            "has_header": False,
            "column_names": _NAMES,
        }
    )
    assert isinstance(reader, CsvFileSource)
    assert reader.has_header is False
    assert list(reader.to_frame().columns) == _NAMES
