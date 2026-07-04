"""Acceptance test 7: subset-then-mask ordering + counts + FK-consistent determinism.

Composition: `run_subset` (engine core, this sprint) writes a filtered Parquet
dataset; that dataset becomes the `sources` of the EXISTING, UNCHANGED mask
path (`decoy_engine.execution.run_pipeline`). Masking never runs on the full
source in a subset job -- only on what `run_subset` wrote.
"""

from __future__ import annotations

import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.subset._api import run_subset
from decoy_engine.subset._types import FanOutPolicy, SeedSpec, SubsetSource
from tests.unit.subset.conftest import make_parquet, rel

_SUBSET_JOB_SEED = b"\x11\x22\x33\x44\x55\x66\x77\x88"


def _hash_col(name: str, namespace: str) -> dict:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _mask_config(customers_path: str, orders_path: str, out_dir, seed: int) -> dict:
    raw = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {
            "customers": {"type": "file", "format": "parquet", "path": customers_path},
            "orders": {"type": "file", "format": "parquet", "path": orders_path},
        },
        "tables": [
            {"name": "customers", "columns": [_hash_col("customer_id", "customer_identity")]},
            {"name": "orders", "columns": [_hash_col("customer_id", "customer_identity")]},
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["customer_id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "preserve",
                "namespace": "customer_identity",
            }
        ],
        "targets": {
            "customers": {
                "type": "file",
                "format": "parquet",
                "path": str(out_dir / "customers_out.parquet"),
            },
            "orders": {
                "type": "file",
                "format": "parquet",
                "path": str(out_dir / "orders_out.parquet"),
            },
        },
    }
    return PipelineConfig.model_validate(raw).model_dump()


def test_subset_then_mask_counts_fk_propagation_and_cross_job_determinism(tmp_path) -> None:
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    n_customers = 60
    customers_path = make_parquet(
        full_dir, "customers", {"customer_id": list(range(1, n_customers + 1))}
    )
    orders_path = make_parquet(
        full_dir,
        "orders",
        {
            "order_id": list(range(1, 121)),
            "customer_id": [(i % n_customers) + 1 for i in range(120)],
        },
    )

    subset_dir = tmp_path / "subset"
    run_subset(
        sources={
            "customers": SubsetSource(path=customers_path, format="parquet"),
            "orders": SubsetSource(path=orders_path, format="parquet"),
        },
        relationships=(
            rel("customers", ("customer_id",), "orders", ("customer_id",), policy="preserve"),
        ),
        seeds=(
            SeedSpec(table="customers", mode="sample", key_columns=("customer_id",), fraction=0.1),
        ),
        policy=FanOutPolicy(),
        job_seed=_SUBSET_JOB_SEED,
        engine_version="test",
        output_dir=subset_dir,
    )

    subset_customers_path = str(subset_dir / "customers.parquet")
    subset_orders_path = str(subset_dir / "orders.parquet")

    mask_seed = 99
    subset_mask_out = tmp_path / "subset_mask_out"
    subset_mask_out.mkdir()
    full_mask_out = tmp_path / "full_mask_out"
    full_mask_out.mkdir()

    subset_config = _mask_config(
        subset_customers_path, subset_orders_path, subset_mask_out, mask_seed
    )
    full_config = _mask_config(customers_path, orders_path, full_mask_out, mask_seed)

    subset_sources = {
        "customers": pq.read_table(subset_customers_path),
        "orders": pq.read_table(subset_orders_path),
    }
    full_sources = {
        "customers": pq.read_table(customers_path),
        "orders": pq.read_table(orders_path),
    }

    subset_result = run_pipeline(subset_config, sources=subset_sources, engine_version="test")
    full_result = run_pipeline(full_config, sources=full_sources, engine_version="test")

    subset_customers_masked = subset_result.outputs["customers"]
    subset_orders_masked = subset_result.outputs["orders"]

    # Heights == subset heights, NOT full-source heights.
    assert subset_customers_masked.num_rows == pq.read_table(subset_customers_path).num_rows
    assert subset_customers_masked.num_rows < n_customers
    assert subset_orders_masked.num_rows == pq.read_table(subset_orders_path).num_rows
    assert subset_orders_masked.num_rows < 120

    # FK propagation within the subset job: masked child FK values are a subset
    # of masked parent PK values.
    masked_parent_ids = set(subset_customers_masked.column("customer_id").to_pylist())
    masked_child_ids = set(subset_orders_masked.column("customer_id").to_pylist())
    assert masked_child_ids <= masked_parent_ids

    # Cross-job determinism: for every surviving original customer_id, the
    # masked value in the subset job equals the masked value in the full job
    # (per-value derive is independent of which rows are present).
    raw_full_customers = pq.read_table(customers_path).column("customer_id").to_pylist()
    raw_subset_customers = pq.read_table(subset_customers_path).column("customer_id").to_pylist()
    masked_full_customers = full_result.outputs["customers"].column("customer_id").to_pylist()
    masked_subset_customers = subset_customers_masked.column("customer_id").to_pylist()

    full_map = dict(zip(raw_full_customers, masked_full_customers, strict=True))
    assert len(raw_subset_customers) == len(masked_subset_customers)
    for raw_id, masked_value in zip(raw_subset_customers, masked_subset_customers, strict=True):
        assert masked_value == full_map[raw_id]
