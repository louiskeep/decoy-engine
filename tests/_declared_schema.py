"""The Arrow schema a config declares for one table, for `run_mask_pipeline_chunked` calls.

Tests whose chunks carry columns the config does not list build the schema by hand instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa

from decoy_engine import declared_columns


def declared_schema(config: Mapping[str, Any], table: str) -> pa.Schema:
    return pa.schema([pa.field(name, pa.string()) for name in declared_columns(config, table)])


def declare_rest(config: dict[str, Any]) -> dict[str, Any]:
    """Fixture helper: list every source column the config leaves out as `passthrough`.

    The gate refuses a column the config does not declare, and a source with no table entry.
    The findings come from the gate's own rule over each source's header or footer, so a
    fixture says what the gate would otherwise refuse and nothing else. Returns `config`.
    """
    from decoy_engine import source_columns
    from decoy_engine.profile import declaration_findings
    from decoy_engine.profile._declared_columns import DeclarationInput

    inputs = [
        DeclarationInput(name, source_columns(desc, table=name), "fixture")
        for name, desc in (config.get("sources") or {}).items()
    ]
    tables = config.setdefault("tables", [])
    for finding in declaration_findings(config, inputs):
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
    """The Arrow schema of every parquet source in `config`, for `source_loader_schemas`."""
    import pyarrow.parquet as pq

    return {
        name: pq.read_schema(desc["path"]) for name, desc in (config.get("sources") or {}).items()
    }
