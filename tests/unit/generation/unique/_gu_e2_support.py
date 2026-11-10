"""Shared builders for the GEN-UNIQUE E2 acceptance tests (test-side only)."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pyarrow as pa

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.plan import compile_plan
from tests.unit._dps_helpers import empty_profile

MARKER = "MARKER-SECRET-VALUE"


def cfg(tables: list[dict[str, Any]], seed: int = 4242) -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": seed},
        "sources": {},
        "tables": tables,
        "targets": {t["name"]: {"type": "file", "format": "csv", "path": "o.csv"} for t in tables},
    }


def table(name: str, rows: int, cols: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"name": name, "row_count": rows, "generate_columns": cols, **extra}


def validate(tables: list[dict[str, Any]]) -> PipelineConfig:
    return PipelineConfig.model_validate(cfg(tables))


def run(tables: list[dict[str, Any]], seed: int = 4242) -> dict[str, pa.Table]:
    plan = compile_plan(cfg(tables, seed), empty_profile(), decoy_engine_version="test")
    return generate_tables(plan)


def col_values(out: dict[str, pa.Table], table_name: str, column: str) -> list[Any]:
    return out[table_name].column(column).to_pylist()


def duplicates(values: list[Any]) -> int:
    nonnull = [v for v in values if v is not None]
    return len(nonnull) - len(set(nonnull))


def pair_duplicates(a: list[Any], b: list[Any]) -> int:
    pairs = [(x, y) for x, y in zip(a, b, strict=True) if x is not None and y is not None]
    return sum(c - 1 for c in Counter(pairs).values())


def error_types(exc_info: Any) -> list[str]:
    return [e["type"] for e in exc_info.value.errors()]


def seq_parent(name: str, rows: int, **extra: Any) -> dict[str, Any]:
    return table(name, rows, [{"name": "id", "type": "sequence", "start": 1, "step": 1, **extra}])


def ref(name: str, parent: str, column: str = "id", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "type": "reference",
        "reference_table": parent,
        "reference_column": column,
        **extra,
    }
