"""Profiler honours FileSource delimited params (has_header/column_names/delimiter).

The engine profiler (scan / STORM) and the CLI mask reader consume the same
FileSource fields. This guards that profiling a headerless file under supplied
names yields the same frame as the equivalent headered file, and that a
non-comma delimiter is respected.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from decoy_engine.profile._readers import CsvFileSource, build_profile_source

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
