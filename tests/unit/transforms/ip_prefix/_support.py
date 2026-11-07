"""Shared helpers for the ip_prefix acceptance tests: configs, direct handler runs, pipelines."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._row_errors import RowError
from decoy_engine.execution._strategies.network._ip_prefix import IpPrefixStrategyHandler
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.profile import ColumnProfile, Profile, TableProfile

ENGINE_VERSION = "0.1.0"
MASK_KEY = bytes(range(32))
KEY_PROVIDER = SecretKeyProvider(b"ip-prefix-acceptance-secret-0123456789")
SENTINEL_V4 = "203.0.113.77"
SENTINEL_BAD = "203.0.113.777"


class FakeCtx:
    """The StrategyContext surface the handler reads: the mask key and the row-error sink."""

    mask_key = MASK_KEY

    def __init__(self) -> None:
        self.row_errors: list[RowError] = []


def seed(
    provider_config: dict[str, Any] | None = None, namespace: str | None = "ip_ns"
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="ip_prefix",
        provider=None,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=tuple(sorted((provider_config or {}).items())),
        coherent_with=(),
    )


def run_handler(
    values: list[Any], provider_config: dict[str, Any] | None = None
) -> tuple[list[Any], FakeCtx]:
    ctx = FakeCtx()
    df = pd.DataFrame({"ip": pd.Series(values, dtype=object)})
    out, warnings = IpPrefixStrategyHandler().run(df, "ip", seed(provider_config), ctx)  # type: ignore[arg-type]
    assert warnings == []
    return out["ip"].tolist(), ctx


def column(name: str = "ip", namespace: str | None = "ip_ns", **provider_config: Any) -> dict:
    col: dict[str, Any] = {"name": name, "strategy": "ip_prefix"}
    if namespace is not None:
        col["namespace"] = namespace
    if provider_config:
        col["provider_config"] = provider_config
    return col


def config(*tables: tuple[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 7},
        "tables": [{"name": name, "columns": cols} for name, cols in tables],
    }


def profile(**tables: list[str]) -> Profile:
    return Profile(
        schema_version=1,
        tables=tuple(
            TableProfile(
                name=name,
                row_count=3,
                columns=tuple(
                    ColumnProfile(
                        name=c,
                        dtype="object",
                        row_count=3,
                        null_count=0,
                        distinct_count=3,
                        sampled=False,
                        is_candidate_key_sampled=False,
                        declared_pk=False,
                        is_fk=False,
                        fk_target=None,
                        pii_class=None,
                    )
                    for c in cols
                ),
            )
            for name, cols in tables.items()
        ),
        relationships=(),
        profiled_at=datetime(2026, 11, 7),
        decoy_engine_version=ENGINE_VERSION,
    )


def write_sources(tmp_path: Path, tables: dict[str, pa.Table]) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    sources: dict[str, Any] = {}
    targets: dict[str, Any] = {}
    for name, table in tables.items():
        path = tmp_path / f"{name}.parquet"
        pq.write_table(table, path)
        sources[name] = {"type": "file", "path": str(path), "format": "parquet"}
        targets[name] = {
            "type": "file",
            "path": str(tmp_path / f"{name}.out.parquet"),
            "format": "parquet",
        }
    return {"sources": sources, "targets": targets}


def pipeline(
    tmp_path: Path,
    tables: dict[str, pa.Table],
    table_columns: dict[str, list[dict[str, Any]]],
    *,
    extra: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    cfg = config(*table_columns.items())
    cfg.update(write_sources(tmp_path, tables))
    cfg.update(extra or {})
    return run_pipeline(
        cfg,
        dict(tables),
        engine_version=ENGINE_VERSION,
        key_provider=KEY_PROVIDER,
        **kwargs,
    )
