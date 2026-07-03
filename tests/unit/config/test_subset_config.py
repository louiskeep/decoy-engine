"""Config-level subset-then-mask enforcement tests (Sprint G, SS5)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from decoy_engine.config import PipelineConfig


def _base_config(tmp_path) -> dict[str, Any]:
    customers_path = str(tmp_path / "customers.parquet")
    orders_path = str(tmp_path / "orders.parquet")
    return {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {
            "customers": {"type": "file", "format": "parquet", "path": customers_path},
            "orders": {"type": "file", "format": "parquet", "path": orders_path},
        },
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {"name": "customer_id", "strategy": "hash", "namespace": "customer_identity"}
                ],
            },
            {
                "name": "orders",
                "columns": [
                    {"name": "customer_id", "strategy": "hash", "namespace": "customer_identity"}
                ],
            },
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
                "path": str(tmp_path / "out_customers.parquet"),
            },
            "orders": {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / "out_orders.parquet"),
            },
        },
    }


def _with_subset(config: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    config = copy.deepcopy(config)
    subset = {
        "seeds": [
            {
                "table": "customers",
                "mode": "sample",
                "key_columns": ["customer_id"],
                "fraction": 0.5,
            }
        ],
    }
    subset.update(overrides)
    config["subset"] = subset
    return config


def test_valid_subset_block_accepted(tmp_path) -> None:
    config = _with_subset(_base_config(tmp_path))
    validated = PipelineConfig.model_validate(config)
    assert validated.subset is not None
    assert validated.subset.seeds[0].table == "customers"


def test_mask_then_subset_rejected_when_source_equals_target(tmp_path) -> None:
    config = _base_config(tmp_path)
    config["sources"]["customers"]["path"] = config["targets"]["customers"]["path"]
    config = _with_subset(config)
    with pytest.raises(Exception) as excinfo:
        PipelineConfig.model_validate(config)
    assert "subsetting runs BEFORE masking" in str(excinfo.value)


def test_subset_seed_naming_undeclared_table_rejected(tmp_path) -> None:
    config = _with_subset(
        _base_config(tmp_path),
        seeds=[{"table": "nonexistent", "mode": "sample", "key_columns": ["id"], "fraction": 0.5}],
    )
    with pytest.raises(Exception) as excinfo:
        PipelineConfig.model_validate(config)
    assert "nonexistent" in str(excinfo.value)


@pytest.mark.parametrize("fraction", [0, 1.5])
def test_seed_fraction_out_of_range_rejected(tmp_path, fraction) -> None:
    config = _with_subset(
        _base_config(tmp_path),
        seeds=[
            {
                "table": "customers",
                "mode": "sample",
                "key_columns": ["customer_id"],
                "fraction": fraction,
            }
        ],
    )
    with pytest.raises(Exception):
        PipelineConfig.model_validate(config)


def test_both_fraction_and_count_rejected(tmp_path) -> None:
    config = _with_subset(
        _base_config(tmp_path),
        seeds=[
            {
                "table": "customers",
                "mode": "sample",
                "key_columns": ["customer_id"],
                "fraction": 0.5,
                "count": 5,
            }
        ],
    )
    with pytest.raises(Exception):
        PipelineConfig.model_validate(config)


def test_non_parquet_source_rejected_when_subset_configured(tmp_path) -> None:
    config = _base_config(tmp_path)
    config["sources"]["customers"]["format"] = "csv"
    config = _with_subset(config)
    with pytest.raises(Exception) as excinfo:
        PipelineConfig.model_validate(config)
    assert "convert to Parquet for subsetting" in str(excinfo.value)


def test_no_subset_block_is_unaffected(tmp_path) -> None:
    config = _base_config(tmp_path)
    config["sources"]["customers"]["format"] = "csv"
    validated = PipelineConfig.model_validate(config)
    assert validated.subset is None
