"""Unit tests for source.file's has_header config setting.

When `has_header: false`, the first row is data and columns get default
names (col_0, col_1, ...). Default behavior (`has_header` absent or
true) keeps the existing first-row-is-header semantics.
"""

import os
import tempfile

import pandas as pd
import pytest

from decoy_engine.graph.ops import source_file
from decoy_engine.internal.validator import ValidationError


@pytest.fixture
def headerless_csv():
    """A CSV with no header row — just data."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w") as f:
        f.write("1,Alice,42\n2,Bob,37\n3,Carol,29\n")
    yield path
    os.unlink(path)


@pytest.fixture
def headered_csv():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    pd.DataFrame({"id": [1, 2, 3], "name": ["Alice", "Bob", "Carol"]}).to_csv(path, index=False)
    yield path
    os.unlink(path)


class TestValidate:
    def test_has_header_true_accepts_no_column_names(self, headerless_csv):
        source_file.validate_config({"path": headerless_csv, "has_header": True})

    def test_has_header_false_without_column_names_passes(self, headerless_csv):
        # Drafting workflow: the user just unchecked the "has headers"
        # checkbox and hasn't filled in column_names yet. validate_config
        # used to block this combo to prevent silent mask no-ops, but
        # that was too aggressive for mid-edit state. The web inspector
        # now nudges inline instead; the real cross-node protection is
        # R2.3's "mask references column the source can't produce".
        source_file.validate_config({"path": headerless_csv, "has_header": False})

    def test_has_header_false_passes_when_column_names_set(self, headerless_csv):
        source_file.validate_config(
            {
                "path": headerless_csv,
                "has_header": False,
                "column_names": ["id", "name", "age"],
            }
        )

    def test_has_header_rejects_non_bool(self, headerless_csv):
        with pytest.raises(ValidationError) as exc:
            source_file.validate_config({"path": headerless_csv, "has_header": "yes"})
        assert "has_header" in (exc.value.path or "")


class TestApplyPandas:
    """The pandas branch is the easier substrate to assert column names
    on — explicitly forces engine=pandas to skip the duckdb path."""

    def test_default_treats_first_row_as_header(self, headered_csv):
        df = source_file.apply([], {"path": headered_csv, "__engine": "pandas"}, None)
        assert list(df.columns) == ["id", "name"]
        assert len(df) == 3

    def test_has_header_false_generates_column_names(self, headerless_csv):
        df = source_file.apply(
            [], {"path": headerless_csv, "has_header": False, "__engine": "pandas"}, None
        )
        # First row should be data, not header
        assert len(df) == 3
        assert list(df.columns) == ["col_0", "col_1", "col_2"]
        assert df["col_0"].tolist() == [1, 2, 3]
        assert df["col_1"].tolist() == ["Alice", "Bob", "Carol"]

    def test_has_header_true_explicit_matches_default(self, headered_csv):
        df = source_file.apply(
            [], {"path": headered_csv, "has_header": True, "__engine": "pandas"}, None
        )
        assert list(df.columns) == ["id", "name"]


class TestApplyDuckDB:
    def test_has_header_false_normalizes_column_names(self, headerless_csv):
        table = source_file.apply(
            [], {"path": headerless_csv, "has_header": False, "__engine": "duckdb"}, None
        )
        assert table.column_names == ["col_0", "col_1", "col_2"]
        assert table.num_rows == 3

    def test_default_uses_header_row(self, headered_csv):
        table = source_file.apply([], {"path": headered_csv, "__engine": "duckdb"}, None)
        assert table.column_names == ["id", "name"]
        assert table.num_rows == 3
