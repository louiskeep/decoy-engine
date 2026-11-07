"""Shared helpers for the email_mask acceptance tests."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine import run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from tests._keyed_secret import THROWAWAY_KEY_PROVIDER

ENGINE_VERSION = "email-mask-test"
MASK_KEY = THROWAWAY_KEY_PROVIDER.mask_key()
OTHER_KEY = bytes(range(32))  # a second 32-byte mask root
NAMESPACE = "ns_email"
LOCAL = r"[0-9a-f]{20}"

# One row per behaviour row in the plan's section 3.1, plus nulls and malformed values.
MIXED: list[Any] = [
    "alice@acme.com",
    "Bob@Gmail.com",
    "x@cs.stanford.edu",
    "y@ox.ac.uk",
    "z@agency.gov",
    "z@dwp.gov.uk",
    "z@army.mil",
    '"john doe"@acme.com',
    "a@b@acme.com",
    "ü@bücher.de",
    None,
    "not-an-email",
    "a@acme.com\n",
    "a@[192.0.2.1]",
    "",
    "alice@acme.com",
]


def handler() -> Any:
    return SCALAR_HANDLERS["email_mask"]


def ctx(mask_key: bytes = MASK_KEY) -> StrategyContext:
    return StrategyContext(
        registry=get_default_registry(),
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=(7).to_bytes(8, "big"),
        mask_key=mask_key,
    )


def seed(provider_config: dict[str, Any], namespace: str | None = NAMESPACE) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="email_mask",
        provider=None,
        backend_type="transform",
        backend_version="v1",
        cardinality_mode="reuse",
        provider_config=tuple(sorted(provider_config.items())),
    )


def run_handler(
    values: list[Any] | pd.Series,
    keep: str = "domain",
    *,
    mask_key: bytes = MASK_KEY,
    namespace: str = NAMESPACE,
) -> tuple[list[Any], list[Any]]:
    frame = pd.DataFrame(
        {"e": values if isinstance(values, pd.Series) else pd.Series(values, dtype=object)}
    )
    out, warnings = handler().run(frame, "e", seed({"keep": keep}, namespace), ctx(mask_key))
    return out["e"].tolist(), warnings


def oracle_local(value: Any, mask_key: bytes = MASK_KEY, namespace: str = NAMESPACE) -> str:
    """The local part computed without the handler: first 20 hex of the keyed HMAC."""
    canonical = unicodedata.normalize("NFC", str(value)).encode("utf-8", "surrogatepass")
    return derive(mask_key, namespace, canonical).hex()[:20]


def column(keep: Any = "domain", **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": "email",
        "strategy": "email_mask",
        "namespace": NAMESPACE,
        "provider_config": {"keep": keep},
    }
    entry.update(extra)
    return entry


def raw_config(columns: list[dict[str, Any]], path: str = "/dev/null") -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 42},
        "sources": {"t": {"type": "file", "format": "parquet", "path": path}},
        "tables": [{"name": "t", "columns": columns}],
        "targets": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
    }


def write(table: pa.Table, path: Path) -> str:
    pq.write_table(table, path)
    return str(path)


def single_table(
    values: list[Any], tmp_path: Path, keep: str = "domain"
) -> tuple[dict[str, Any], pa.Table]:
    source = pa.table({"email": pa.array(values, type=pa.string())})
    cfg = PipelineConfig.model_validate(
        raw_config([column(keep)], path=write(source, tmp_path / "s.parquet"))
    ).model_dump()
    return cfg, source


def run_single(values: list[Any], tmp_path: Path, keep: str = "domain", **kwargs: Any) -> list[Any]:
    cfg, source = single_table(values, tmp_path, keep)
    kwargs.setdefault("auto_chunk", False)
    result = run_pipeline(cfg, sources={"t": source}, engine_version=ENGINE_VERSION, **kwargs)
    return result.outputs["t"].column("email").to_pylist()
