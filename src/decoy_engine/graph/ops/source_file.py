"""source.file — read a CSV / Parquet / fixed-width file into a DataFrame.

Config:
    path: str            - filesystem path
    format: 'csv' | 'parquet' | 'fixed_width'
                           (optional for csv/parquet; inferred from extension.
                            fixed_width must be set explicitly.)
    has_header: bool     - CSV only; defaults to true. When false, column
                           names are auto-generated (col_0, col_1, ...) and
                           the first row is treated as data, unless
                           `column_names` is also set.
    column_names: list[str]
                           CSV only; optional; requires has_header=false.
                           Replaces the auto-generated col_0..col_N names
                           with the user-supplied list. Length must
                           match the file's actual column count or pandas
                           raises a parse error the caller surfaces as
                           an OpError.
    delimiter: str       - CSV only; the field separator. When omitted,
                           pandas/DuckDB auto-detect.
    delimiter_is_regex: bool
                           CSV only; when true, the delimiter string is
                           treated as a regex. Pandas python engine only;
                           DuckDB falls back to the pandas branch.
    strip_quotes: bool   - CSV only; default true. When false, quote
                           characters are treated as literal content
                           (csv.QUOTE_NONE).
    encoding: str        - CSV only; default 'utf-8'.
    row_limit: int       - cap on rows returned. Applies to all formats.
                           Also honors the existing internal preview hint
                           __preview_row_limit when row_limit is unset.
    fw_columns: list[dict]
                           fixed_width only; required. Each entry is
                           {name: str, start: int (1-based), length: int}.

Phase 4 of the polars-duckdb hybrid plan: NATIVE_ENGINE='duckdb' for
csv and parquet. The DuckDB path streams natively (no need for the
dead chunked-CSV iterator the cheap-wins memo planned to wire) and
uses query optimizer pushdown for parquet column projection. Pandas
fallback retained for graph engine mode = pandas, and is also the
substrate for fixed_width regardless of NATIVE_ENGINE since DuckDB
has no fixed-width scanner.
"""

import csv as _csv
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "source.file"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (0, 0)
OUTPUT_KIND = "stream"

_CSV_PARAM_KEYS = (
    "delimiter", "delimiter_is_regex", "strip_quotes", "encoding", "column_names",
)


def validate_config(config: dict[str, Any]) -> None:
    if "path" not in config:
        raise ValidationError("missing required field 'path'", "config.path")
    fmt = (config.get("format") or _infer_format(config["path"])).lower()
    if fmt not in {"csv", "parquet", "fixed_width"}:
        raise ValidationError(
            f"unsupported format {fmt!r} (csv|parquet|fixed_width)",
            "config.format",
        )

    if "has_header" in config and not isinstance(config["has_header"], bool):
        raise ValidationError(
            "'has_header' must be a boolean when set", "config.has_header"
        )

    if "row_limit" in config:
        rl = config["row_limit"]
        if not isinstance(rl, int) or isinstance(rl, bool) or rl <= 0:
            raise ValidationError(
                "'row_limit' must be a positive integer when set",
                "config.row_limit",
            )

    if fmt == "csv":
        if "delimiter" in config:
            d = config["delimiter"]
            if not isinstance(d, str) or d == "":
                raise ValidationError(
                    "'delimiter' must be a non-empty string when set",
                    "config.delimiter",
                )
        if "delimiter_is_regex" in config and not isinstance(
            config["delimiter_is_regex"], bool
        ):
            raise ValidationError(
                "'delimiter_is_regex' must be a boolean when set",
                "config.delimiter_is_regex",
            )
        if "strip_quotes" in config and not isinstance(config["strip_quotes"], bool):
            raise ValidationError(
                "'strip_quotes' must be a boolean when set",
                "config.strip_quotes",
            )
        if "encoding" in config:
            enc = config["encoding"]
            if not isinstance(enc, str) or enc == "":
                raise ValidationError(
                    "'encoding' must be a non-empty string when set",
                    "config.encoding",
                )
        if "column_names" in config:
            names = config["column_names"]
            if not isinstance(names, list) or not names:
                raise ValidationError(
                    "'column_names' must be a non-empty list of strings when set",
                    "config.column_names",
                )
            for i, name in enumerate(names):
                if not isinstance(name, str) or name == "":
                    raise ValidationError(
                        f"column_names[{i}] must be a non-empty string",
                        f"config.column_names[{i}]",
                    )
            # column_names overrides auto-generated col_0..col_N; only
            # meaningful when there's no header row to read names from.
            if config.get("has_header", True):
                raise ValidationError(
                    "'column_names' requires has_header=false",
                    "config.column_names",
                )
        elif config.get("has_header") is False:
            # has_header=false without column_names produces DuckDB auto-
            # generated 'column0', 'column1', ... which downstream masks
            # configured by name silently no-op against. Block at validate
            # time so the user fixes the source rather than discovering it
            # via "Column 'x' not found in DataFrame" warnings on a run
            # that produced an unmasked output file.
            raise ValidationError(
                "no header columns defined: set has_header=true (read names "
                "from the file's first row) or provide column_names explicitly",
                "config.has_header",
            )
    else:
        # csv-only params don't belong on parquet/fixed_width sources.
        for key in _CSV_PARAM_KEYS:
            if key in config:
                raise ValidationError(
                    f"'{key}' applies to format='csv' only, not {fmt!r}",
                    f"config.{key}",
                )
        if "has_header" in config and fmt != "csv":
            raise ValidationError(
                f"'has_header' applies to format='csv' only, not {fmt!r}",
                "config.has_header",
            )

    if fmt == "fixed_width":
        cols = config.get("fw_columns")
        if not isinstance(cols, list) or not cols:
            raise ValidationError(
                "fixed_width requires 'fw_columns' (non-empty list)",
                "config.fw_columns",
            )
        for i, col in enumerate(cols):
            if not isinstance(col, dict):
                raise ValidationError(
                    f"fw_columns[{i}] must be an object", f"config.fw_columns[{i}]"
                )
            name = col.get("name")
            start = col.get("start")
            length = col.get("length")
            if not isinstance(name, str) or name == "":
                raise ValidationError(
                    f"fw_columns[{i}].name must be a non-empty string",
                    f"config.fw_columns[{i}].name",
                )
            if not isinstance(start, int) or isinstance(start, bool) or start < 1:
                raise ValidationError(
                    f"fw_columns[{i}].start must be a positive 1-based integer",
                    f"config.fw_columns[{i}].start",
                )
            if not isinstance(length, int) or isinstance(length, bool) or length < 1:
                raise ValidationError(
                    f"fw_columns[{i}].length must be a positive integer",
                    f"config.fw_columns[{i}].length",
                )
    elif "fw_columns" in config:
        raise ValidationError(
            f"'fw_columns' applies to format='fixed_width' only, not {fmt!r}",
            "config.fw_columns",
        )


def apply(inputs, config, ctx):
    engine = config.get("__engine", "pandas")
    path = Path(config["path"])
    fmt = (config.get("format") or _infer_format(str(path))).lower()
    # DuckDB has no fixed-width scanner; route through pandas regardless of
    # the requested __engine so callers don't need to know the substrate.
    if fmt == "fixed_width":
        engine = "pandas"

    if engine == "duckdb":
        result = _apply_duckdb(config)
        row_count = result.num_rows
        column_count = len(result.column_names)
    else:
        result = _apply_pandas(config)
        row_count = len(result)
        column_count = len(result.columns)
    if ctx is not None and hasattr(ctx, "export"):
        ctx.export("row_count", int(row_count))
        ctx.export("column_count", int(column_count))
        ctx.export("inferred_format", fmt)
        try:
            ctx.export("file_size_bytes", int(path.stat().st_size))
        except OSError:
            # File might be a stream / FUSE mount that doesn't stat cleanly.
            # Skip rather than fail the op for a metric.
            pass
    return result


def _resolve_row_limit(config: dict[str, Any]) -> int | None:
    # row_limit is the user-facing field; __preview_row_limit is the
    # internal hint the preview path sets. Either may be present; if both
    # are, take the smaller cap so neither override silently expands the
    # other.
    user_limit = config.get("row_limit")
    preview_limit = config.get("__preview_row_limit")
    candidates = [v for v in (user_limit, preview_limit) if v]
    return min(candidates) if candidates else None


def _apply_pandas(config: dict[str, Any]) -> pd.DataFrame:
    path = Path(config["path"])
    fmt = (config.get("format") or _infer_format(str(path))).lower()
    row_limit = _resolve_row_limit(config)
    try:
        if fmt == "csv":
            return _read_csv_pandas(path, config, row_limit)
        if fmt == "parquet":
            df = pd.read_parquet(path)
            return df.head(row_limit) if row_limit else df
        if fmt == "fixed_width":
            return _read_fwf_pandas(path, config, row_limit)
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"failed to read {path}: {exc}") from exc
    raise OpError(f"unsupported format: {fmt}")


def _read_csv_pandas(
    path: Path, config: dict[str, Any], row_limit: int | None
) -> pd.DataFrame:
    has_header = config.get("has_header", True)
    delimiter = config.get("delimiter")
    delimiter_is_regex = bool(config.get("delimiter_is_regex"))
    strip_quotes = bool(config.get("strip_quotes", True))
    encoding = config.get("encoding", "utf-8")
    column_names = config.get("column_names")

    kwargs: dict[str, Any] = {"nrows": row_limit, "encoding": encoding}
    if not has_header:
        kwargs["header"] = None
    if delimiter is not None:
        kwargs["sep"] = delimiter
        if delimiter_is_regex:
            # The python engine is required for regex separators; the C
            # engine raises ParserError on multi-char / regex sep values.
            kwargs["engine"] = "python"
    if not strip_quotes:
        kwargs["quoting"] = _csv.QUOTE_NONE

    df = pd.read_csv(path, **kwargs)
    if not has_header:
        if isinstance(column_names, list) and column_names:
            # User-supplied names override the col_0..col_N default.
            # Length mismatch is a real config error; surface it as an
            # OpError via the caller's try/except.
            if len(column_names) != len(df.columns):
                raise OpError(
                    f"column_names has {len(column_names)} entries but the "
                    f"file has {len(df.columns)} columns"
                )
            df.columns = list(column_names)
        else:
            # header=None tells pandas there's no header row; column labels
            # default to RangeIndex which serializes as 0,1,2,... Rename to
            # col_0, col_1, ... so downstream node configs that reference
            # column names by string survive (numeric labels stringify in
            # surprising ways when serialized to YAML / CSV).
            df.columns = [f"col_{i}" for i in range(len(df.columns))]
    return df


def _read_fwf_pandas(
    path: Path, config: dict[str, Any], row_limit: int | None
) -> pd.DataFrame:
    fw_columns = config["fw_columns"]
    # fw_columns uses 1-based start + length; pandas read_fwf wants
    # 0-based half-open (start_inclusive, end_exclusive). Convert here.
    colspecs = [(c["start"] - 1, c["start"] - 1 + c["length"]) for c in fw_columns]
    names = [c["name"] for c in fw_columns]
    # dtype=str keeps the raw substring per column boundary so leading
    # zeros, sentinel codes, and padding survive intact. This matches
    # STORM's fixed-width loader (api/analytics/router.py:_load_fw_from_bytes),
    # which slices the line bytes verbatim without type inference.
    # Without dtype=str pandas would silently coerce `"001"` -> 1 and a
    # pipeline reading the same file would see different values than
    # the scan that profiled it. Downstream nodes can cast explicitly
    # if they want numeric typing.
    kwargs: dict[str, Any] = {
        "colspecs": colspecs, "names": names, "header": None, "dtype": str,
    }
    if row_limit:
        kwargs["nrows"] = row_limit
    return pd.read_fwf(path, **kwargs)


def _apply_duckdb(config: dict[str, Any]) -> pa.Table:
    import duckdb

    path = Path(config["path"])
    fmt = (config.get("format") or _infer_format(str(path))).lower()
    row_limit = _resolve_row_limit(config)

    has_header = config.get("has_header", True)
    delimiter = config.get("delimiter")
    delimiter_is_regex = bool(config.get("delimiter_is_regex"))
    strip_quotes = bool(config.get("strip_quotes", True))
    encoding = config.get("encoding", "utf-8")

    if fmt == "csv" and delimiter_is_regex:
        # DuckDB's read_csv has no regex separator option. Fall back to
        # pandas and arrow-convert. This is rare and the pandas path
        # already handles the regex case correctly.
        df = _read_csv_pandas(path, config, row_limit)
        return pa.Table.from_pandas(df, preserve_index=False)

    try:
        # In-memory DuckDB connection per op: cheap, isolated, GC'd at the
        # end of apply(). The relation is materialized to Arrow before the
        # connection closes.
        con = duckdb.connect(":memory:")
        try:
            if fmt == "csv":
                opts = [f"header={'true' if has_header else 'false'}"]
                if delimiter is not None:
                    opts.append(f"delim='{_escape_sql_literal(delimiter)}'")
                if not strip_quotes:
                    # quote='' disables quoting in read_csv so quote chars
                    # are treated as literal content.
                    opts.append("quote=''")
                if encoding and encoding.lower() != "utf-8":
                    # DuckDB read_csv only supports utf-8 / latin-1 / utf-16.
                    # Anything else, route through pandas and arrow-convert.
                    if encoding.lower() not in {"latin-1", "latin1", "utf-16"}:
                        df = _read_csv_pandas(path, config, row_limit)
                        return pa.Table.from_pandas(df, preserve_index=False)
                    opts.append(f"encoding='{_escape_sql_literal(encoding)}'")
                sql = f"SELECT * FROM read_csv('{_escape_sql_literal(str(path))}', {', '.join(opts)})"
            elif fmt == "parquet":
                sql = f"SELECT * FROM read_parquet('{_escape_sql_literal(str(path))}')"
            else:
                raise OpError(f"unsupported format: {fmt}")
            if row_limit:
                sql += f" LIMIT {int(row_limit)}"
            # to_arrow_table returns pa.Table; .arrow() returns a
            # RecordBatchReader which isn't what the runner cache wants.
            table = con.execute(sql).to_arrow_table()
            if fmt == "csv" and not has_header:
                column_names = config.get("column_names")
                if isinstance(column_names, list) and column_names:
                    if len(column_names) != table.num_columns:
                        raise OpError(
                            f"column_names has {len(column_names)} entries but the "
                            f"file has {table.num_columns} columns"
                        )
                    table = table.rename_columns(list(column_names))
                else:
                    # Normalize DuckDB's `column0`/`column1` to `col_0`/`col_1`
                    # so downstream configs reference the same names regardless
                    # of which substrate read the file.
                    table = table.rename_columns(
                        [f"col_{i}" for i in range(table.num_columns)]
                    )
            return table
        finally:
            con.close()
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"failed to read {path}: {exc}") from exc


def _escape_sql_literal(value: str) -> str:
    # Single-quote escaping for inline DuckDB SQL literals. Paths on
    # Windows can contain backslashes and apostrophes never appear in
    # well-formed delimiter strings, but we doubled-escape here to keep
    # the query unambiguous.
    return value.replace("'", "''")


def _infer_format(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"parquet", "pq"}:
        return "parquet"
    return "csv"
