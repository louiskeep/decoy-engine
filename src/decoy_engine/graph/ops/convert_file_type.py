"""convert.file_type: re-encode the upstream Arrow Table to a different on-disk
format without leaving the pipeline.

Sits naturally between a `source.file` (which reads one shape) and a
`target.file` (which writes another) to make format conversion *explicit*
in the YAML — the canvas tile sits under Transforms, not Targets. Writes
the upstream input to disk in the configured format as a side effect, then
passes the same Arrow Table downstream unchanged so subsequent nodes still
see the data.

Roadmap: Item 57 (Format-convert graph op) and Item 66(b) — the explicit
counterpart to source/target format-mismatch detection. Engine work is
small because DuckDB already encodes every format in the launch set:
COPY ... TO with FORMAT {CSV, PARQUET, JSON}, plus a delimiter override
for TSV.

Config:
    format: 'csv' | 'tsv' | 'parquet' | 'jsonl'   required, target format
    output_filename: str                            filesystem path to write

NATIVE_ENGINE='duckdb' for symmetry with target.file — both wrap COPY ... TO
so the runtime cost is identical, and the runner has already materialized
the upstream into pa.Table by the time apply() runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "convert.file_type"
NATIVE_ENGINE = "duckdb"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"

_SUPPORTED_FORMATS = ("csv", "tsv", "parquet", "jsonl")


def validate_config(config: dict[str, Any]) -> None:
    fmt = config.get("format")
    if not isinstance(fmt, str) or not fmt.strip():
        raise ValidationError(
            f"missing required field 'format' ({'|'.join(_SUPPORTED_FORMATS)})",
            "config.format",
        )
    if fmt.lower() not in _SUPPORTED_FORMATS:
        raise ValidationError(
            f"unsupported format {fmt!r} ({'|'.join(_SUPPORTED_FORMATS)})",
            "config.format",
        )
    if "output_filename" not in config:
        raise ValidationError(
            "missing required field 'output_filename'",
            "config.output_filename",
        )


def apply(inputs, config, ctx):
    table: pa.Table = inputs[0]

    # Preview mode: skip the side-effect write so previewing a graph
    # doesn't litter the filesystem with intermediate files. Mirrors the
    # target.file convention.
    if config.get("__preview_row_limit") is not None:
        return table

    import duckdb

    fmt = config["format"].lower()
    path = Path(config["output_filename"])
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        con = duckdb.connect(":memory:")
        try:
            con.register("in_table", table)
            copy_clause = _copy_clause(fmt)
            con.execute(f"COPY (SELECT * FROM in_table) TO '{path}' {copy_clause}")
        finally:
            con.close()
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"convert.file_type: failed to write {path} as {fmt}: {exc}") from exc

    # Stream semantics: pass the input through unchanged so downstream
    # nodes still see the data. target.file returns slice(0, 0) because
    # it terminates the pipeline; we don't.
    return table


def _copy_clause(fmt: str) -> str:
    """Build the DuckDB COPY format clause for the requested encoding."""
    if fmt == "csv":
        return "(FORMAT CSV, HEADER)"
    if fmt == "tsv":
        return "(FORMAT CSV, HEADER, DELIMITER '\t')"
    if fmt == "parquet":
        return "(FORMAT PARQUET)"
    if fmt == "jsonl":
        # DuckDB's JSON COPY writes one object per line by default
        # (newline-delimited JSON / JSON Lines). ARRAY=false makes the
        # intent explicit and survives a future DuckDB default change.
        return "(FORMAT JSON, ARRAY false)"
    raise OpError(f"unsupported format: {fmt}")
