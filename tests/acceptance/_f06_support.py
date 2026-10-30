"""Shared fixtures for the F06 declared-casting acceptance, property and security suites.

Every symbol under test is imported inside a function body, so a missing F06 symbol fails the
one test that needs it instead of failing collection for the whole file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa

INT64 = {"id": "integer/ascii/v1", "signed": True, "bit_width": 64}
DEC_5_2 = {"id": "decimal/plain/v1", "precision": 5, "scale": 2}
ISO_DATE = {"id": "date/iso8601/v1"}
USD = {
    "id": "currency/explicit/v1",
    "currency_code": "USD",
    "token": "$",
    "token_position": "prefix",
    "sign_style": "leading_minus",
    "precision": 12,
    "scale": 2,
    "radix": ".",
    "group": ",",
}
EUR = {
    "id": "currency/explicit/v1",
    "currency_code": "EUR",
    "token": " EUR",
    "token_position": "suffix",
    "sign_style": "leading_minus",
    "precision": 12,
    "scale": 2,
    "radix": ",",
    "group": ".",
}
PROTECTED_QUARANTINE = {
    "schema_version": 1,
    "access_policy": "tenant-job-owner",
    "retention_seconds": 604800,
    "max_rows": 1000,
    "max_bytes": 1_000_000,
    "required_protection": "encrypted",
}


def zero_pad(width: int) -> dict[str, Any]:
    return {
        "id": "integer/zero-pad/v1",
        "width": width,
        "width_unit": "code_points",
        "pad": "0",
        "align": "right",
        "signed": True,
        "bit_width": 64,
    }


def column(name: str, strategy: str = "passthrough", **fields: Any) -> dict[str, Any]:
    return {"name": name, "strategy": strategy, **fields}


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> str:
    lines = [",".join(header)] + [",".join(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def config(
    columns: list[dict[str, Any]],
    *,
    source_path: str,
    table: str = "people",
    top: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {table: {"type": "file", "format": "csv", "path": source_path}},
        "tables": [{"name": table, "columns": columns}],
        "targets": {table: {"type": "file", "format": "csv", "path": source_path + ".out"}},
        "relationships": [],
        "namespaces": {},
        **(top or {}),
    }


def validate(raw: dict[str, Any]) -> dict[str, Any]:
    from decoy_engine.config import PipelineConfig

    return PipelineConfig.model_validate(raw).model_dump()


def run(raw: dict[str, Any], sources: dict[str, Any], **kwargs: Any) -> Any:
    from decoy_engine.execution._pipeline import run_pipeline

    return run_pipeline(validate(raw), sources, engine_version="f06-test", **kwargs)


def text_table(**cols: list[Any]) -> pa.Table:
    return pa.table({k: pa.array(v, pa.string()) for k, v in cols.items()})
