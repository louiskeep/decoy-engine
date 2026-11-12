"""Fixtures for subset-inside-a-run tests: customers -> orders -> items, parquet or csv."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.profile._lazy_csv import CsvLazySource
from decoy_engine.profile._readers import LazySource
from decoy_engine.subset import select_subset, subset_request_from_config
from tests._declared_schema import declare_rest

VAR = "SUB_RUN_TEST_SECRET"
CANARY = "CANARY91357"
KEYS = {"customers": "cid", "orders": "oid", "items": "iid"}


def tables(*, canary: bool = False) -> dict[str, pa.Table]:
    cids = [f"C{i}" for i in range(30)]
    if canary:
        cids[7] = CANARY
    return {
        "customers": pa.table({"cid": cids, "name": [f"n{i}" for i in range(30)]}),
        "orders": pa.table(
            {"oid": [f"O{i}" for i in range(90)], "cid": [cids[i % 30] for i in range(90)]}
        ),
        "items": pa.table(
            {"iid": [f"I{i}" for i in range(180)], "oid": [f"O{i % 90}" for i in range(180)]}
        ),
    }


def _col(name: str, ns: str) -> dict[str, str]:
    return {"name": name, "strategy": "hash", "namespace": ns}


def _rel(parent: str, pcol: str, child: str, ccol: str, ns: str) -> dict[str, Any]:
    return {
        "parent": {"table": parent, "columns": [pcol]},
        "children": [{"table": child, "columns": [ccol]}],
        "orphan_policy": "preserve",
        "namespace": ns,
    }


def job(
    tmp_path: Path, *, fmt: str = "parquet", seed: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, pa.Table], dict[str, Any]]:
    """The validated config dump, the resident tables and the lazy sources (written to disk)."""
    data = tables(canary=seed is not None and seed.get("mode") == "keys")
    sources: dict[str, Any] = {}
    for name, table in data.items():
        path = tmp_path / f"{name}.{fmt}"
        if fmt == "parquet":
            pq.write_table(table, path)
        else:
            pacsv.write_csv(table, path)
        sources[name] = {"type": "file", "format": fmt, "path": str(path)}
        if fmt == "csv":
            sources[name]["has_header"] = True
    raw: dict[str, Any] = {
        "version": 2,
        "global_settings": {"seed": 1, "mask_secret_ref": f"env:{VAR}"},
        "sources": sources,
        "tables": [
            {"name": "customers", "columns": [_col("cid", "cust")]},
            {"name": "orders", "columns": [_col("oid", "ord"), _col("cid", "cust")]},
            {"name": "items", "columns": [_col("oid", "ord")]},
        ],
        "relationships": [
            _rel("customers", "cid", "orders", "cid", "cust"),
            _rel("orders", "oid", "items", "oid", "ord"),
        ],
        "targets": {
            n: {"type": "file", "format": "parquet", "path": str(tmp_path / f"out_{n}.parquet")}
            for n in data
        },
        "subset": {
            "seeds": seed
            or {"table": "customers", "mode": "sample", "key_columns": ["cid"], "count": 5}
        },
    }
    raw["subset"]["seeds"] = [raw["subset"]["seeds"]]
    cfg = PipelineConfig.model_validate(declare_rest(raw)).model_dump()
    return cfg, data, lazy_sources(cfg)


def lazy_sources(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, desc in cfg["sources"].items():
        if desc["format"] == "csv":
            out[name] = CsvLazySource.from_descriptor(desc, table=name)
        else:
            out[name] = LazySource(Path(desc["path"]))
    return out


def kept(cfg: dict[str, Any], data: dict[str, pa.Table]) -> dict[str, list[int]]:
    """Source rows the standalone selection keeps, per table (whole tables: every row)."""
    request = subset_request_from_config(cfg)
    assert request is not None
    sel = select_subset(request, data, config=cfg)
    return {n: sorted(sel.survivors.get(n, range(t.num_rows))) for n, t in data.items()}
