"""Unit tests for source.file's R1.2 parsing-control extensions.

Covers the new config fields that bring source.file to parity with the
STORM trigger UI's parsing controls: delimiter, delimiter_is_regex,
strip_quotes, encoding, row_limit, and the fixed_width format with
fw_columns. Each control is exercised on both the pandas and duckdb
substrates where the substrate supports it; controls that only work on
one substrate document the fallback.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.graph.ops import source_file
from decoy_engine.internal.validator import ValidationError


# ── shared fixtures ─────────────────────────────────────────────────────────

def _write_tmp(content: str, suffix: str = ".csv", encoding: str = "utf-8") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(content)
    return path


@pytest.fixture
def semicolon_csv():
    path = _write_tmp("id;name;amount\n1;Alice;10\n2;Bob;20\n")
    yield path
    os.unlink(path)


@pytest.fixture
def pipe_csv():
    path = _write_tmp("id|name|amount\n1|Alice|10\n2|Bob|20\n")
    yield path
    os.unlink(path)


@pytest.fixture
def multispace_csv():
    # Regex separator: one or more spaces. Stable test data without
    # commas / tabs to avoid ambiguity with default sniffers.
    path = _write_tmp("id name amount\n1   Alice   10\n2 Bob 20\n")
    yield path
    os.unlink(path)


@pytest.fixture
def quoted_csv():
    # Default strip_quotes=true: the quotes get stripped, value is Alice.
    # strip_quotes=false (QUOTE_NONE): the quotes stay as literal content.
    path = _write_tmp('id,name\n1,"Alice"\n2,"Bob"\n')
    yield path
    os.unlink(path)


@pytest.fixture
def latin1_csv():
    # 'café' as bytes: 63 61 66 e9 0a in latin-1, NOT valid utf-8.
    path = _write_tmp("id,name\n1,caf\xe9\n", encoding="latin-1")
    yield path
    os.unlink(path)


@pytest.fixture
def fixed_width_file():
    # Columns: id (1-3), name (4-13), amount (14-19). 1-based starts.
    # Three rows.
    content = (
        "001Alice     000100\n"
        "002Bob       000200\n"
        "003Carol     000300\n"
    )
    path = _write_tmp(content, suffix=".txt")
    yield path
    os.unlink(path)


@pytest.fixture
def large_csv():
    """50-row CSV for row_limit checks."""
    rows = "id,value\n" + "".join(f"{i},{i * 10}\n" for i in range(50))
    path = _write_tmp(rows)
    yield path
    os.unlink(path)


# ── validate_config ─────────────────────────────────────────────────────────

class TestValidateNewFields:
    def test_delimiter_accepts_non_empty_string(self, semicolon_csv):
        source_file.validate_config({"path": semicolon_csv, "delimiter": ";"})

    def test_delimiter_rejects_empty_string(self, semicolon_csv):
        with pytest.raises(ValidationError) as exc:
            source_file.validate_config({"path": semicolon_csv, "delimiter": ""})
        assert "delimiter" in (exc.value.path or "")

    def test_delimiter_rejects_non_string(self, semicolon_csv):
        with pytest.raises(ValidationError):
            source_file.validate_config({"path": semicolon_csv, "delimiter": 9})

    def test_delimiter_is_regex_accepts_bool(self, semicolon_csv):
        source_file.validate_config(
            {"path": semicolon_csv, "delimiter": r"\s+", "delimiter_is_regex": True}
        )

    def test_delimiter_is_regex_rejects_non_bool(self, semicolon_csv):
        with pytest.raises(ValidationError):
            source_file.validate_config(
                {"path": semicolon_csv, "delimiter_is_regex": "yes"}
            )

    def test_strip_quotes_accepts_bool(self, semicolon_csv):
        source_file.validate_config({"path": semicolon_csv, "strip_quotes": False})

    def test_strip_quotes_rejects_non_bool(self, semicolon_csv):
        with pytest.raises(ValidationError):
            source_file.validate_config({"path": semicolon_csv, "strip_quotes": "yes"})

    def test_encoding_accepts_non_empty_string(self, semicolon_csv):
        source_file.validate_config({"path": semicolon_csv, "encoding": "latin-1"})

    def test_encoding_rejects_empty_string(self, semicolon_csv):
        with pytest.raises(ValidationError):
            source_file.validate_config({"path": semicolon_csv, "encoding": ""})

    def test_row_limit_accepts_positive_int(self, semicolon_csv):
        source_file.validate_config({"path": semicolon_csv, "row_limit": 100})

    def test_row_limit_rejects_zero(self, semicolon_csv):
        with pytest.raises(ValidationError):
            source_file.validate_config({"path": semicolon_csv, "row_limit": 0})

    def test_row_limit_rejects_negative(self, semicolon_csv):
        with pytest.raises(ValidationError):
            source_file.validate_config({"path": semicolon_csv, "row_limit": -1})

    def test_row_limit_rejects_bool(self, semicolon_csv):
        # bool is a subclass of int; explicitly reject so True/False can't
        # silently mean "limit=1" or "no limit".
        with pytest.raises(ValidationError):
            source_file.validate_config({"path": semicolon_csv, "row_limit": True})


class TestValidateFormatScoping:
    def test_csv_only_params_rejected_on_parquet(self, tmp_path):
        pq = tmp_path / "x.parquet"
        pd.DataFrame({"a": [1]}).to_parquet(pq)
        for key, val in [
            ("delimiter", ";"),
            ("delimiter_is_regex", True),
            ("strip_quotes", False),
            ("encoding", "utf-8"),
            ("has_header", False),
        ]:
            with pytest.raises(ValidationError, match=key):
                source_file.validate_config({"path": str(pq), key: val})

    def test_csv_only_params_rejected_on_fixed_width(self, fixed_width_file):
        base = {
            "path": fixed_width_file,
            "format": "fixed_width",
            "fw_columns": [{"name": "a", "start": 1, "length": 3}],
        }
        for key, val in [
            ("delimiter", ";"),
            ("strip_quotes", False),
            ("encoding", "utf-8"),
        ]:
            with pytest.raises(ValidationError, match=key):
                source_file.validate_config({**base, key: val})

    def test_fw_columns_rejected_on_csv(self, semicolon_csv):
        with pytest.raises(ValidationError, match="fw_columns"):
            source_file.validate_config(
                {
                    "path": semicolon_csv,
                    "fw_columns": [{"name": "a", "start": 1, "length": 3}],
                }
            )


class TestValidateFixedWidth:
    def test_accepts_minimal_fw_columns(self, fixed_width_file):
        source_file.validate_config(
            {
                "path": fixed_width_file,
                "format": "fixed_width",
                "fw_columns": [{"name": "id", "start": 1, "length": 3}],
            }
        )

    def test_requires_fw_columns_when_fixed_width(self, fixed_width_file):
        with pytest.raises(ValidationError, match="fw_columns"):
            source_file.validate_config(
                {"path": fixed_width_file, "format": "fixed_width"}
            )

    def test_rejects_empty_fw_columns(self, fixed_width_file):
        with pytest.raises(ValidationError):
            source_file.validate_config(
                {"path": fixed_width_file, "format": "fixed_width", "fw_columns": []}
            )

    def test_rejects_missing_name(self, fixed_width_file):
        with pytest.raises(ValidationError, match=r"fw_columns\[0\]\.name"):
            source_file.validate_config(
                {
                    "path": fixed_width_file,
                    "format": "fixed_width",
                    "fw_columns": [{"start": 1, "length": 3}],
                }
            )

    def test_rejects_zero_start(self, fixed_width_file):
        # 1-based; 0 is not a valid start position.
        with pytest.raises(ValidationError, match=r"fw_columns\[0\]\.start"):
            source_file.validate_config(
                {
                    "path": fixed_width_file,
                    "format": "fixed_width",
                    "fw_columns": [{"name": "a", "start": 0, "length": 3}],
                }
            )

    def test_rejects_negative_length(self, fixed_width_file):
        with pytest.raises(ValidationError, match=r"fw_columns\[0\]\.length"):
            source_file.validate_config(
                {
                    "path": fixed_width_file,
                    "format": "fixed_width",
                    "fw_columns": [{"name": "a", "start": 1, "length": 0}],
                }
            )

    def test_rejects_unsupported_format(self, semicolon_csv):
        with pytest.raises(ValidationError, match="format"):
            source_file.validate_config(
                {"path": semicolon_csv, "format": "tsv"}
            )


# ── apply: csv parsing controls ─────────────────────────────────────────────

@pytest.mark.parametrize("engine", ["pandas", "duckdb"])
class TestCsvDelimiter:
    def test_semicolon_delimiter(self, semicolon_csv, engine):
        result = source_file.apply(
            [],
            {"path": semicolon_csv, "delimiter": ";", "__engine": engine},
            None,
        )
        if engine == "duckdb":
            assert result.column_names == ["id", "name", "amount"]
            assert result.num_rows == 2
        else:
            assert list(result.columns) == ["id", "name", "amount"]
            assert len(result) == 2
            assert result["name"].tolist() == ["Alice", "Bob"]

    def test_pipe_delimiter(self, pipe_csv, engine):
        result = source_file.apply(
            [],
            {"path": pipe_csv, "delimiter": "|", "__engine": engine},
            None,
        )
        cols = result.column_names if engine == "duckdb" else list(result.columns)
        assert cols == ["id", "name", "amount"]


class TestCsvDelimiterRegex:
    """Regex separator works on pandas; duckdb path falls back to pandas
    internally and arrow-converts."""

    def test_regex_pandas(self, multispace_csv):
        df = source_file.apply(
            [],
            {
                "path": multispace_csv,
                "delimiter": r"\s+",
                "delimiter_is_regex": True,
                "__engine": "pandas",
            },
            None,
        )
        assert list(df.columns) == ["id", "name", "amount"]
        assert df["name"].tolist() == ["Alice", "Bob"]

    def test_regex_duckdb_falls_back_to_pandas(self, multispace_csv):
        table = source_file.apply(
            [],
            {
                "path": multispace_csv,
                "delimiter": r"\s+",
                "delimiter_is_regex": True,
                "__engine": "duckdb",
            },
            None,
        )
        assert isinstance(table, pa.Table)
        assert table.column_names == ["id", "name", "amount"]


@pytest.mark.parametrize("engine", ["pandas", "duckdb"])
class TestCsvStripQuotes:
    def test_strip_quotes_default_strips(self, quoted_csv, engine):
        result = source_file.apply(
            [], {"path": quoted_csv, "__engine": engine}, None
        )
        names = (
            result.column("name").to_pylist()
            if engine == "duckdb"
            else result["name"].tolist()
        )
        assert names == ["Alice", "Bob"]

    def test_strip_quotes_false_keeps_quotes(self, quoted_csv, engine):
        result = source_file.apply(
            [],
            {"path": quoted_csv, "strip_quotes": False, "__engine": engine},
            None,
        )
        names = (
            result.column("name").to_pylist()
            if engine == "duckdb"
            else result["name"].tolist()
        )
        assert names == ['"Alice"', '"Bob"']


class TestCsvEncoding:
    def test_latin1_pandas(self, latin1_csv):
        df = source_file.apply(
            [],
            {"path": latin1_csv, "encoding": "latin-1", "__engine": "pandas"},
            None,
        )
        assert df["name"].tolist() == ["café"]

    def test_latin1_duckdb_falls_back_when_non_native(self, latin1_csv):
        # DuckDB read_csv supports utf-8/latin-1/utf-16 natively; latin-1
        # uses the duckdb path. We assert behavior is correct, not which
        # substrate handled it.
        result = source_file.apply(
            [],
            {"path": latin1_csv, "encoding": "latin-1", "__engine": "duckdb"},
            None,
        )
        assert result.column("name").to_pylist() == ["café"]

    def test_unsupported_encoding_falls_back_via_duckdb(self):
        # cp1252 is not in DuckDB's native list; the op should fall back
        # to pandas (which supports it) and arrow-convert the result.
        path = _write_tmp("id,name\n1,Alice\n", encoding="cp1252")
        try:
            table = source_file.apply(
                [],
                {"path": path, "encoding": "cp1252", "__engine": "duckdb"},
                None,
            )
            assert isinstance(table, pa.Table)
            assert table.column("name").to_pylist() == ["Alice"]
        finally:
            os.unlink(path)


# ── apply: row_limit ────────────────────────────────────────────────────────

@pytest.mark.parametrize("engine", ["pandas", "duckdb"])
class TestRowLimit:
    def test_row_limit_caps_output(self, large_csv, engine):
        result = source_file.apply(
            [], {"path": large_csv, "row_limit": 5, "__engine": engine}, None
        )
        rows = result.num_rows if engine == "duckdb" else len(result)
        assert rows == 5

    def test_row_limit_takes_min_with_preview_limit(self, large_csv, engine):
        # When both are set, the tighter cap wins so neither knob silently
        # widens the other.
        result = source_file.apply(
            [],
            {
                "path": large_csv,
                "row_limit": 20,
                "__preview_row_limit": 3,
                "__engine": engine,
            },
            None,
        )
        rows = result.num_rows if engine == "duckdb" else len(result)
        assert rows == 3


# ── apply: fixed-width ──────────────────────────────────────────────────────

class TestFixedWidth:
    """Fixed-width always routes through pandas regardless of __engine
    because DuckDB has no fixed-width scanner. Tests assert correct
    behavior and DataFrame return type for both engine selections."""

    @pytest.mark.parametrize("engine", ["pandas", "duckdb"])
    def test_reads_3_columns_3_rows(self, fixed_width_file, engine):
        df = source_file.apply(
            [],
            {
                "path": fixed_width_file,
                "format": "fixed_width",
                "fw_columns": [
                    {"name": "id", "start": 1, "length": 3},
                    {"name": "name", "start": 4, "length": 10},
                    {"name": "amount", "start": 14, "length": 6},
                ],
                "__engine": engine,
            },
            None,
        )
        assert list(df.columns) == ["id", "name", "amount"]
        assert len(df) == 3
        # Fixed-width preserves the raw column substring as a string
        # (dtype=str on read_fwf) so leading zeros and padding survive
        # the read. Downstream nodes cast explicitly when numeric
        # typing is wanted.
        assert df["id"].tolist() == ["001", "002", "003"]
        assert [n.strip() for n in df["name"].tolist()] == ["Alice", "Bob", "Carol"]
        assert df["amount"].tolist() == ["000100", "000200", "000300"]

    def test_row_limit_honored(self, fixed_width_file):
        df = source_file.apply(
            [],
            {
                "path": fixed_width_file,
                "format": "fixed_width",
                "fw_columns": [
                    {"name": "id", "start": 1, "length": 3},
                    {"name": "name", "start": 4, "length": 10},
                    {"name": "amount", "start": 14, "length": 6},
                ],
                "row_limit": 2,
                "__engine": "pandas",
            },
            None,
        )
        assert len(df) == 2
        assert df["id"].tolist() == ["001", "002"]


# ── apply: format inference ─────────────────────────────────────────────────

class TestFormatInference:
    def test_parquet_extension_inferred(self, tmp_path):
        pq = tmp_path / "data.parquet"
        pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"]}).to_parquet(pq)
        df = source_file.apply([], {"path": str(pq), "__engine": "pandas"}, None)
        assert list(df.columns) == ["id", "name"]
        assert len(df) == 2

    def test_explicit_format_overrides_extension(self, tmp_path):
        # File has .txt extension but is actually CSV content.
        path = tmp_path / "looks_like_text.txt"
        path.write_text("id,name\n1,Alice\n")
        df = source_file.apply(
            [],
            {"path": str(path), "format": "csv", "__engine": "pandas"},
            None,
        )
        assert list(df.columns) == ["id", "name"]


# ── export hooks ────────────────────────────────────────────────────────────

class _CaptureCtx:
    def __init__(self) -> None:
        self.exports: dict[str, object] = {}

    def export(self, key: str, value: object) -> None:
        self.exports[key] = value


class TestExports:
    def test_inferred_format_export_includes_fixed_width(self, fixed_width_file):
        ctx = _CaptureCtx()
        source_file.apply(
            [],
            {
                "path": fixed_width_file,
                "format": "fixed_width",
                "fw_columns": [
                    {"name": "id", "start": 1, "length": 3},
                    {"name": "name", "start": 4, "length": 10},
                    {"name": "amount", "start": 14, "length": 6},
                ],
                "__engine": "pandas",
            },
            ctx,
        )
        assert ctx.exports.get("inferred_format") == "fixed_width"
        assert ctx.exports.get("row_count") == 3
        assert ctx.exports.get("column_count") == 3

    def test_export_row_count_reflects_row_limit(self, large_csv):
        ctx = _CaptureCtx()
        source_file.apply(
            [],
            {"path": large_csv, "row_limit": 7, "__engine": "pandas"},
            ctx,
        )
        assert ctx.exports.get("row_count") == 7


# ── housekeeping: bool subtype guard ────────────────────────────────────────

@pytest.fixture
def headerless_csv():
    path = _write_tmp("1,Alice,10\n2,Bob,20\n3,Carol,30\n")
    yield path
    os.unlink(path)


class TestValidateColumnNames:
    def test_accepts_non_empty_list_of_strings(self, headerless_csv):
        source_file.validate_config({
            "path": headerless_csv,
            "has_header": False,
            "column_names": ["id", "name", "amount"],
        })

    def test_requires_has_header_false(self, headerless_csv):
        # column_names overrides the auto-generated col_0..N names, which
        # only happen when there is no header. Setting both is a config
        # contradiction; validator rejects rather than silently letting
        # one path or the other win.
        with pytest.raises(ValidationError, match="column_names"):
            source_file.validate_config({
                "path": headerless_csv,
                "has_header": True,
                "column_names": ["a", "b"],
            })
        # Default has_header=True also rejects.
        with pytest.raises(ValidationError, match="column_names"):
            source_file.validate_config({
                "path": headerless_csv,
                "column_names": ["a", "b"],
            })

    def test_rejects_empty_list(self, headerless_csv):
        with pytest.raises(ValidationError):
            source_file.validate_config({
                "path": headerless_csv, "has_header": False, "column_names": [],
            })

    def test_rejects_non_string_entries(self, headerless_csv):
        with pytest.raises(ValidationError, match=r"column_names\[1\]"):
            source_file.validate_config({
                "path": headerless_csv, "has_header": False,
                "column_names": ["id", 99, "amount"],
            })

    def test_rejects_empty_string_entries(self, headerless_csv):
        with pytest.raises(ValidationError, match=r"column_names\[0\]"):
            source_file.validate_config({
                "path": headerless_csv, "has_header": False,
                "column_names": ["", "name"],
            })

    def test_rejected_on_parquet(self, tmp_path):
        pq = tmp_path / "x.parquet"
        pd.DataFrame({"a": [1]}).to_parquet(pq)
        with pytest.raises(ValidationError, match="column_names"):
            source_file.validate_config({
                "path": str(pq), "column_names": ["a", "b"],
            })


@pytest.mark.parametrize("engine", ["pandas", "duckdb"])
class TestApplyColumnNames:
    def test_overrides_auto_generated_names(self, headerless_csv, engine):
        result = source_file.apply(
            [],
            {
                "path": headerless_csv,
                "has_header": False,
                "column_names": ["id", "name", "amount"],
                "__engine": engine,
            },
            None,
        )
        cols = result.column_names if engine == "duckdb" else list(result.columns)
        assert cols == ["id", "name", "amount"]

    def test_length_mismatch_raises_operror(self, headerless_csv, engine):
        from decoy_engine.graph.ops._base import OpError
        with pytest.raises(OpError, match="column_names has"):
            source_file.apply(
                [],
                {
                    "path": headerless_csv,
                    "has_header": False,
                    "column_names": ["id", "name"],  # file has 3 columns
                    "__engine": engine,
                },
                None,
            )

    def test_falls_back_to_col_N_when_names_omitted(self, headerless_csv, engine):
        result = source_file.apply(
            [],
            {"path": headerless_csv, "has_header": False, "__engine": engine},
            None,
        )
        cols = result.column_names if engine == "duckdb" else list(result.columns)
        assert cols == ["col_0", "col_1", "col_2"]


class TestBoolSubtypeGuard:
    """Python's bool is a subclass of int. The validator must reject bool
    where it expects a strict int (row_limit, fw_columns start/length) so
    True doesn't get silently coerced into 1."""

    def test_row_limit_bool_rejected(self, fixed_width_file):
        with pytest.raises(ValidationError):
            source_file.validate_config({"path": fixed_width_file, "row_limit": False})

    def test_fw_start_bool_rejected(self, fixed_width_file):
        with pytest.raises(ValidationError):
            source_file.validate_config(
                {
                    "path": fixed_width_file,
                    "format": "fixed_width",
                    "fw_columns": [{"name": "a", "start": True, "length": 3}],
                }
            )
