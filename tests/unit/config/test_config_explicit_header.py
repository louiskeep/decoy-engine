"""CONFIG-EXPLICIT E1: a csv source must say whether row one is a header.

AT-E1-1 (missing `has_header` refused), AT-E1-2 (explicit value round-trips), AT-E1-3 (each
dialect branch raises its registered code with the one text) and AT-E1-8 (an old dump fails
with the fix line, and the fixed dump reads as the old default did).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from decoy_engine import reasons
from decoy_engine.config import PipelineConfig
from decoy_engine.config._csv_dialect import csv_dialect_problem
from decoy_engine.config._sources import FILE_SOURCE_PATH_FIELDS, FileSource
from decoy_engine.errors import ConfigError
from decoy_engine.profile import _csv_reader
from decoy_engine.profile._csv_reader import resolve_csv_dialect
from decoy_engine.profile._readers import build_profile_source

TABLE = "zz_table_marker"
PATH = "zz_path_marker.csv"


def _csv(**extra: Any) -> dict[str, Any]:
    return {"type": "file", "format": "csv", "path": PATH, **extra}


def _pipeline(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {TABLE: source},
        "tables": [{"name": TABLE, "source": TABLE, "columns": []}],
        "targets": {TABLE: {"type": "file", "format": "csv", "path": "out.csv"}},
    }


def _one_error(source: dict[str, Any]) -> dict[str, Any]:
    with pytest.raises(ValidationError) as info:
        FileSource.model_validate(source)
    errors = info.value.errors()
    assert len(errors) == 1, errors
    return dict(errors[0])


# AT-E1-1


def test_csv_without_has_header_is_refused() -> None:
    error = _one_error(_csv())
    assert error["type"] == reasons.config.SOURCE_FILE_HAS_HEADER_REQUIRED
    assert error["loc"][-1] == "has_header"
    assert error["input"] is None
    assert "has_header: true" in error["msg"] and "has_header: false" in error["msg"]
    with pytest.raises(ValidationError) as info:
        PipelineConfig.model_validate(_pipeline(_csv()))
    coded = [
        e
        for e in info.value.errors()
        if e["type"] == reasons.config.SOURCE_FILE_HAS_HEADER_REQUIRED
    ]
    assert len(coded) == 1
    assert TABLE not in coded[0]["msg"] and PATH not in coded[0]["msg"]


def test_an_invalid_format_without_has_header_reports_only_the_format() -> None:
    error = _one_error({"type": "file", "format": "tsv", "path": PATH})
    assert error["loc"] == ("format",)


# AT-E1-2


@pytest.mark.parametrize(
    "extra",
    [
        {"has_header": True},
        {"has_header": False, "column_names": ["a", "b"]},
        {"has_header": False, "column_names_file": "names.csv"},
    ],
    ids=["header", "inline-names", "header-file"],
)
def test_explicit_has_header_round_trips(extra: dict[str, Any]) -> None:
    source = FileSource.model_validate(_csv(**extra))
    dump = source.model_dump()
    assert dump["has_header"] is extra["has_header"]
    again = FileSource.model_validate(dump)
    assert again.model_dump_json() == source.model_dump_json()


def test_a_parquet_dump_carries_no_csv_dialect_field() -> None:
    dump = FileSource.model_validate(
        {"type": "file", "format": "parquet", "path": "p.parquet"}
    ).model_dump()
    assert "has_header" not in dump and "column_names_file" not in dump
    assert "layout" in dump


def test_resolver_refuses_a_descriptor_without_has_header_before_any_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = tmp_path / "names.csv"
    names.write_text("a,b\n")
    reads: list[object] = []
    monkeypatch.setattr(_csv_reader.fileaccess, "read_checked", lambda *a, **k: reads.append(a))
    for descriptor in ({}, {"has_header": None}, {"column_names_file": str(names)}):
        with pytest.raises(ConfigError) as info:
            resolve_csv_dialect(descriptor)
        assert str(info.value) == (
            "resolve_csv_dialect: has_header must be true or false; "
            "validate the descriptor with PipelineConfig first"
        )
    assert reads == []


# AT-E1-3

_CODED = [
    (
        {"has_header": False, "column_names": ["a"], "column_names_file": "n.csv"},
        reasons.config.SOURCE_FILE_COLUMN_NAMES_SOURCE_CONFLICT,
    ),
    ({"has_header": False}, reasons.config.SOURCE_FILE_NO_HEADER_COLUMNS),
    ({"has_header": False, "column_names": []}, reasons.config.SOURCE_FILE_NO_HEADER_COLUMNS),
    (
        {"has_header": True, "column_names": ["a"]},
        reasons.config.SOURCE_FILE_COLUMN_NAMES_WITH_HEADER,
    ),
    (
        {"has_header": True, "column_names_file": "n.csv"},
        reasons.config.SOURCE_FILE_COLUMN_NAMES_WITH_HEADER,
    ),
]


@pytest.mark.parametrize(
    ("extra", "code"),
    _CODED,
    ids=["both-sources", "neither", "empty-names", "names-with-header", "file-with-header"],
)
def test_dialect_branch_codes(extra: dict[str, Any], code: str) -> None:
    error = _one_error(_csv(**extra))
    assert error["type"] == code
    problem = csv_dialect_problem(
        extra["has_header"],
        extra.get("column_names"),
        ",",
        column_names_file=extra.get("column_names_file"),
    )
    assert error["msg"] == f"FileSource: {problem}"


_LAYOUT = {"columns": [{"name": "id", "start": 0, "width": 4}]}


@pytest.mark.parametrize(
    "source",
    [
        {"type": "file", "format": "parquet", "path": "p.parquet", "column_names_file": "n.csv"},
        {"type": "file", "format": "parquet", "path": "p.parquet", "has_header": True},
        {"type": "file", "format": "fixed_width", "path": "f", "layout": _LAYOUT, "delimiter": "|"},
        {
            "type": "file",
            "format": "fixed_width",
            "path": "f",
            "layout": _LAYOUT,
            "column_names_file": "n.csv",
        },
    ],
    ids=["parquet-file", "parquet-header", "fw-delimiter", "fw-file"],
)
def test_dialect_fields_on_a_non_csv_source_are_coded(source: dict[str, Any]) -> None:
    error = _one_error(source)
    assert error["type"] == reasons.config.SOURCE_FILE_CSV_PARAM_ON_NON_CSV
    assert "only valid when format='csv'" in error["msg"]


def test_a_bad_delimiter_stays_an_uncoded_value_error() -> None:
    error = _one_error(_csv(has_header=True, delimiter="||"))
    assert error["type"] == "value_error"


# AT-E1-8


def test_old_dump_fails_with_fix_line(tmp_path: Path) -> None:
    path = tmp_path / "people.csv"
    path.write_text("id,name\n1,Ada\n2,Grace\n")
    old = {"type": "file", "format": "csv", "path": str(path)}
    error = _one_error(old)
    assert error["type"] == reasons.config.SOURCE_FILE_HAS_HEADER_REQUIRED
    assert "add `has_header: true` if the first row names the columns" in error["msg"]
    fixed = FileSource.model_validate({**old, "has_header": True}).model_dump()
    frame = build_profile_source(fixed).to_frame()
    expected = pd.DataFrame({"id": [1, 2], "name": ["Ada", "Grace"]})
    pd.testing.assert_frame_equal(frame.reset_index(drop=True), expected, check_dtype=False)


def test_file_source_path_fields_name_every_path_marked_field() -> None:
    # The CLI and platform resolve and contain relative paths by this tuple.
    assert FILE_SOURCE_PATH_FIELDS == ("path", "column_names_file")
