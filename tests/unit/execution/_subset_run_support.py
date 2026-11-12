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

VAR = "SUB_STOP_TEST_SECRET"
CANARY = "CANARY91357"
KEYS = {"customers": "cid", "orders": "oid", "items": "iid"}


# One column per value-keyed strategy: a kept row gets the full run's value, whichever
# position it holds in the subset.
WIDE_COLUMNS: list[dict[str, Any]] = [
    {
        "name": "email",
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "mail",
        "cardinality_mode": "reuse",
        "provider_config": {"pool_size": 20},
    },
    {
        "name": "tier",
        "strategy": "categorical",
        "deterministic": True,
        "namespace": "tier",
        "provider_config": {"categories": ["free", "pro", "team"], "weights": [0.6, 0.3, 0.1]},
    },
    {
        "name": "phone",
        "strategy": "fpe",
        "namespace": "ph",
        "provider_config": {"charset": "digits"},
    },
    {"name": "note", "strategy": "redact"},
    {"name": "zip", "strategy": "truncate", "provider_config": {"length": 3}},
    {
        "name": "dob",
        "strategy": "date_shift",
        "namespace": "dob",
        "provider_config": {"min_days": -30, "max_days": 30, "date_format": "%Y-%m-%d"},
    },
    {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
    {
        "name": "income",
        "strategy": "top_code",
        "provider_config": {"cap": 80, "over_label": "OVER"},
    },
    {"name": "keep", "strategy": "passthrough"},
]
WIDE_VALUES: dict[str, list[Any]] = {
    "email": [f"person{i}@source.example" for i in range(30)],
    "tier": [["bronze", "silver", "gold"][i % 3] for i in range(30)],
    "phone": [f"{i:09d}" for i in range(30)],
    "note": [f"secret-{i}" for i in range(30)],
    "zip": [f"{10000 + i:05d}" for i in range(30)],
    "dob": [f"19{60 + (i % 40):02d}-03-{1 + (i % 28):02d}" for i in range(30)],
    "age": [(i * 7) % 90 for i in range(30)],
    "income": [(i * 13) % 100 for i in range(30)],
    "keep": [f"keep-{i}" for i in range(30)],
}


def tables(
    *, canary: bool = False, int_keys: bool = False, wide: bool = False
) -> dict[str, pa.Table]:
    cids: list[Any] = [f"C{i}" for i in range(30)]
    if canary:
        cids[7] = CANARY
    if int_keys:
        cids = list(range(100, 130))
    order_cids = [cids[i % 30] for i in range(90)]
    if int_keys:
        order_cids[3] = None  # a null foreign key
    customers = pa.table({"cid": cids, "name": [f"n{i}" for i in range(30)]})
    if wide:
        for column, values in WIDE_VALUES.items():
            customers = customers.append_column(column, pa.array(values))
    return {
        "customers": customers,
        "orders": pa.table({"oid": [f"O{i}" for i in range(90)], "cid": order_cids}),
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
    tmp_path: Path,
    *,
    fmt: str = "parquet",
    seed: dict[str, Any] | None = None,
    int_keys: bool = False,
    wide: bool = False,
) -> tuple[dict[str, Any], dict[str, pa.Table], dict[str, Any]]:
    """The validated config dump, the resident tables and the lazy sources (written to disk)."""
    data = tables(
        canary=seed is not None and seed.get("mode") == "keys", int_keys=int_keys, wide=wide
    )
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
            {
                "name": "customers",
                "columns": [_col("cid", "cust"), *(WIDE_COLUMNS if wide else [])],
            },
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
