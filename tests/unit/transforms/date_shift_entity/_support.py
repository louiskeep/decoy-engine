"""Shared builders for the date_shift entity_key acceptance tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

SEED = (0x42).to_bytes(8, "big")
NS = "patient_dates"
ENGINE_VERSION = "entity-date-offset-test"


def seed(namespace: str | None = NS, **cfg: Any) -> ColumnSeed:
    cfg.setdefault("date_format", "%Y-%m-%d")
    return ColumnSeed(
        namespace=namespace,
        strategy="date_shift",
        provider="date_shift",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=tuple(cfg.items()),
        coherent_with=(),
        when=None,
    )


def ctx(snapshots: dict[str, pd.Series] | None = None, table: str = "t") -> StrategyContext:
    out = StrategyContext(
        registry=None,  # type: ignore[arg-type]
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=SEED,
    )
    object.__setattr__(out, "current_table", table)
    if snapshots:
        object.__setattr__(
            out, "group_anchor_snapshots", {(table, k): v for k, v in snapshots.items()}
        )
    return out


def date_col(name: str = "d", **cfg: Any) -> dict[str, Any]:
    cfg.setdefault("date_format", "%Y-%m-%d")
    entry: dict[str, Any] = {
        "name": name,
        "strategy": "date_shift",
        "namespace": cfg.pop("namespace", NS),
        "provider_config": cfg,
    }
    return entry


def passthrough(name: str) -> dict[str, Any]:
    return {"name": name, "strategy": "passthrough"}


def config(
    tmp: Path,
    tables: dict[str, list[dict[str, Any]]],
    relationships: list[dict[str, Any]] | None = None,
    fmt: str = "parquet",
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 2,
        "global_settings": {"seed": 42},
        "sources": {
            t: {"type": "file", "format": fmt, "path": str(tmp / f"{t}.{fmt}")} for t in tables
        },
        "tables": [{"name": t, "columns": cols} for t, cols in tables.items()],
        "targets": {
            t: {"type": "file", "format": fmt, "path": str(tmp / f"{t}_out.{fmt}")} for t in tables
        },
    }
    if relationships:
        raw["relationships"] = relationships
    return PipelineConfig.model_validate(raw).model_dump()


def write_sources(tmp: Path, sources: dict[str, pa.Table]) -> None:
    for name, table in sources.items():
        pq.write_table(table, tmp / f"{name}.parquet")


def run(tmp: Path, cfg: dict[str, Any], sources: dict[str, pa.Table], **kw: Any) -> Any:
    write_sources(tmp, sources)
    return run_pipeline(cfg, sources=sources, engine_version=ENGINE_VERSION, **kw)
