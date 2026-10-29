"""Small real jobs for the F29 integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.config import PipelineConfig

ENGINE_VERSION = "f29-integration"


def fk_job(
    tmp_path: Path, *, mask_secret_ref: str | None = None
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """A two-table FK job: customers.id -> orders.customer_id, hashed under one namespace."""
    customers = pd.DataFrame({"id": [f"C{i}" for i in range(4)]})
    orders = pd.DataFrame({"customer_id": ["C1", "C2", "C1"]})
    cp, op = tmp_path / "customers.csv", tmp_path / "orders.csv"
    customers.to_csv(cp, index=False)
    orders.to_csv(op, index=False)
    settings: dict[str, Any] = {"seed": 42}
    if mask_secret_ref is not None:
        settings["mask_secret_ref"] = mask_secret_ref
    cfg = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": settings,
            "sources": {
                "customers": {"type": "file", "format": "csv", "path": str(cp)},
                "orders": {"type": "file", "format": "csv", "path": str(op)},
            },
            "tables": [
                {
                    "name": "customers",
                    "columns": [{"name": "id", "strategy": "hash", "namespace": "id_ns"}],
                },
                {
                    "name": "orders",
                    "columns": [{"name": "customer_id", "strategy": "hash", "namespace": "id_ns"}],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "customers", "columns": ["id"]},
                    "children": [{"table": "orders", "columns": ["customer_id"]}],
                    "orphan_policy": "preserve",
                    "namespace": "id_ns",
                }
            ],
            "targets": {
                "customers": {"type": "file", "format": "csv", "path": str(tmp_path / "c_out.csv")},
                "orders": {"type": "file", "format": "csv", "path": str(tmp_path / "o_out.csv")},
            },
        }
    ).model_dump()
    return cfg, {
        "customers": pa.Table.from_pandas(customers, preserve_index=False),
        "orders": pa.Table.from_pandas(orders, preserve_index=False),
    }
