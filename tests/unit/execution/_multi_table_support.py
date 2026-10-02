"""Shared builders for the B7 multi-table dispatch acceptance tests.

Plan: docs/plans/2026-10-01-multi-table-dispatch.md (revision 3). Every test needs the
same things: a multi-table job whose source files match the Arrow tables it passes, a
way to run "the split-off run" (the kill switch), the single-table reference of one
table (guarantee 3 (c)), and spies on the split executor. They live here once.

Row counts are tiny on purpose: `THRESHOLD` is 10 rows, `CHUNK` 16, so a `BIG` table of 40
rows dispatches in three chunks (an uneven last one) and a `SMALL` table of 5 stays below
the threshold. Date and time columns run with the default `use_byte_estimate_routing=True`
because the byte-estimate defect B2 recorded is fixed on main (PR #189).
"""

from __future__ import annotations

import copy
import importlib
import inspect
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from tests.unit.execution import _auto_chunk_support as support

ENGINE_VERSION = "b7-multi-table-test"
BIG = support.ROWS
SMALL = 5
CHUNK = support.CHUNK
THRESHOLD = support.THRESHOLD
MODULE = "decoy_engine.execution._pipeline_multi_table"

Columns = list[dict[str, Any]]
TableSpec = tuple[Columns, pa.Table]


def split_supported() -> bool:
    return "multi_table_dispatch_enabled" in inspect.signature(run_pipeline).parameters


def off_kw() -> dict[str, Any]:
    """The split-off run: today's behavior for a multi-table job."""
    return {"multi_table_dispatch_enabled": False} if split_supported() else {}


def kw(**extra: Any) -> dict[str, Any]:
    return {
        "engine_version": ENGINE_VERSION,
        "auto_chunk_threshold_rows": THRESHOLD,
        "chunk_size_rows": CHUNK,
        **extra,
    }


def string_table(n: int = BIG, tag: str = "u") -> pa.Table:
    """The standard two-column source: `h` hashed, `r` redacted."""
    return pa.table(
        {
            "h": pa.array([f"{tag}{i}@x.example" for i in range(n)]),
            "r": pa.array([f"s{tag}{i}" for i in range(n)]),
        }
    )


def std_columns(namespace: str) -> Columns:
    return [support.hash_col("h", namespace), support.redact_col("r")]


def build_job(
    tmp_path: Path,
    tables: Mapping[str, TableSpec],
    *,
    seed: int = 42,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """A validated config over `tables` (name -> (columns, arrow table)) plus the sources.

    The parquet file each `sources` entry names holds the same data as the Arrow table the
    test passes, because `profile_source` reads the file and the execution reads the table."""
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {},
        "tables": [],
        "targets": {},
    }
    sources: dict[str, pa.Table] = {}
    for name, (columns, table) in tables.items():
        path = support.write_source(table, tmp_path / f"{name}.parquet")
        raw["sources"][name] = {"type": "file", "format": "parquet", "path": path}
        raw["tables"].append({"name": name, "columns": columns})
        raw["targets"][name] = {"type": "file", "format": "parquet", "path": "/dev/null"}
        sources[name] = table
    raw.update(extra or {})
    return PipelineConfig.model_validate(raw).model_dump(), sources


def restrict(cfg: dict[str, Any], table: str) -> dict[str, Any]:
    """Guarantee 3 (c)'s reference config: the job restricted to one table."""
    out = copy.deepcopy(cfg)
    out["tables"] = [t for t in out["tables"] if t["name"] == table]
    out["sources"] = {k: v for k, v in out["sources"].items() if k == table}
    out["targets"] = {k: v for k, v in out["targets"].items() if k == table}
    return out


def single_reference(
    cfg: dict[str, Any], sources: Mapping[str, pa.Table], table: str, **extra: Any
) -> Any:
    return run_pipeline(restrict(cfg, table), sources={table: sources[table]}, **kw(**extra))


def spy_split(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Calls into `run_multi_table_split`; empty before the module exists."""
    calls: list[Any] = []
    try:
        mod = importlib.import_module(MODULE)
    except ModuleNotFoundError:
        return calls
    real = mod.run_multi_table_split

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "run_multi_table_split", spy)
    return calls


def spy_adapter_run(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

    calls: list[Any] = []
    real = PandasExecutionAdapter.run

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(PandasExecutionAdapter, "run", spy)
    return calls


def dispatched_tables(result: Any) -> list[str]:
    block = result.quality_metrics.get("auto_chunk", {})
    return [t["table"] for t in block.get("tables", []) if t.get("dispatched")]


def route_of(result: Any, table: str) -> dict[str, Any] | None:
    return (result.quality_metrics.get("chunked_route_by_table") or {}).get(table)


def normalized_metrics(result: Any) -> dict[str, Any]:
    """`quality_metrics` without the split's own evidence keys, for byte-for-byte
    comparisons between no-split runs and the split-off run."""
    return dict(result.quality_metrics)


def timing_keys(result: Any) -> list[tuple[str, str]]:
    return sorted((t.strategy_type, t.column) for t in result.timings)


def same_error(a: BaseException, b: BaseException) -> bool:
    return type(a) is type(b) and getattr(a, "code", None) == getattr(b, "code", None)
