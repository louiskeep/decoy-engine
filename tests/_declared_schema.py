"""The Arrow schema a config declares for one table, for `run_mask_pipeline_chunked` calls.

Tests whose chunks carry columns the config does not list build the schema by hand instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa

from decoy_engine import declared_columns
from decoy_engine.profile._declared_columns import arrow_columns


def declared_schema(config: Mapping[str, Any], table: str) -> pa.Schema:
    return pa.schema([pa.field(name, pa.string()) for name in declared_columns(config, table)])


def chunked_entry_kwargs(entry: Any, config: Mapping[str, Any], table: str) -> dict[str, Any]:
    """`source_schema` for `run_mask_pipeline_chunked`, the one chunked entry that requires it.

    For tests that loop over the chunked entries by function; the other entries take nothing.
    """
    if getattr(entry, "__name__", "") == "run_mask_pipeline_chunked":
        return {"source_schema": declared_schema(config, table)}
    return {}


def declare_rest(
    config: dict[str, Any], supplied: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Fixture helper: list every source column the config leaves out as `passthrough`.

    The gate refuses a column the config does not declare, and a source with no table entry.
    The findings come from the gate's own rule over each source's header or footer (and over
    the `supplied` tables, anything with an Arrow `schema`), so a fixture says what the gate
    would otherwise refuse and nothing else. Returns `config`.
    """
    from decoy_engine import source_columns
    from decoy_engine.profile import declaration_findings
    from decoy_engine.profile._declared_columns import DeclarationInput

    inputs = []
    for name, desc in (config.get("sources") or {}).items():
        try:
            inputs.append(DeclarationInput(name, source_columns(desc, table=name), "fixture"))
        except Exception:
            continue
    inputs += [
        DeclarationInput(name, arrow_columns(src.schema), "fixture")
        for name, src in (supplied or {}).items()
    ]
    tables = config.setdefault("tables", [])
    for finding in declaration_findings(config, inputs):
        if finding.kind not in ("table", "column"):
            continue
        entries = [
            {"name": n, "strategy": "passthrough"} for n in dict.fromkeys(finding.undeclared)
        ]
        if finding.kind == "table":
            tables.append({"name": finding.table, "columns": entries})
            continue
        entry = next(t for t in tables if t.get("name") == finding.table)
        entry["columns"] = [*(entry.get("columns") or []), *entries]
    return config


def file_schemas(config: dict[str, Any]) -> dict[str, pa.Schema]:
    """The Arrow schema of every file source in `config`, for `source_loader_schemas`."""
    import pyarrow.parquet as pq

    from decoy_engine import source_columns

    return {
        name: (
            pq.read_schema(desc["path"])
            if desc.get("format") == "parquet"
            else pa.schema([pa.field(c, pa.string()) for c in source_columns(desc, table=name)])
        )
        for name, desc in (config.get("sources") or {}).items()
    }
