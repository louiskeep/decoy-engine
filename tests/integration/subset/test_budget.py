"""Acceptance test 3: fan-out budget hard-fails BEFORE any materialization."""

from __future__ import annotations

import pytest

from decoy_engine.subset._api import plan_subset, run_subset
from decoy_engine.subset._errors import SubsetBudgetExceededError
from decoy_engine.subset._types import FanOutBudget, FanOutPolicy, SeedSpec, SubsetSource
from tests.unit.subset.conftest import JOB_SEED, make_parquet, rel


def _fixture(tmp_path):
    customers_path = make_parquet(tmp_path, "customers", {"id": [1]})
    orders_path = make_parquet(
        tmp_path, "orders", {"id": list(range(1000)), "customer_id": [1] * 1000}
    )
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",)),)
    seeds = (SeedSpec(table="customers", mode="sample", key_columns=("id",), fraction=1.0),)
    return sources, relationships, seeds


def test_total_row_budget_hard_fails_before_materialization(tmp_path) -> None:
    sources, relationships, seeds = _fixture(tmp_path)
    out_dir = tmp_path / "out"
    with pytest.raises(SubsetBudgetExceededError) as excinfo:
        run_subset(
            sources=sources,
            relationships=relationships,
            seeds=seeds,
            policy=FanOutPolicy(budget=FanOutBudget(max_total_rows=100)),
            job_seed=JOB_SEED,
            engine_version="test",
            output_dir=out_dir,
        )
    err = excinfo.value
    assert err.code == "subset_budget_exceeded"
    assert err.scope == "total"
    assert err.actual > 100
    assert err.edge_id == "customers.id -> orders.customer_id"
    assert "No output was written" in str(err)
    assert err.edge_id in str(err)

    assert not out_dir.exists()
    parquet_files = sorted(p.name for p in tmp_path.rglob("*.parquet"))
    assert parquet_files == ["customers.parquet", "orders.parquet"]  # fixture inputs only


def test_per_table_seed_multiple_budget(tmp_path) -> None:
    sources, relationships, seeds = _fixture(tmp_path)
    out_dir = tmp_path / "out2"
    with pytest.raises(SubsetBudgetExceededError) as excinfo:
        run_subset(
            sources=sources,
            relationships=relationships,
            seeds=seeds,
            policy=FanOutPolicy(budget=FanOutBudget(max_table_seed_multiple=10.0)),
            job_seed=JOB_SEED,
            engine_version="test",
            output_dir=out_dir,
        )
    err = excinfo.value
    assert err.scope == "table"
    assert err.table == "orders"
    assert err.cap == 10
    assert not out_dir.exists()


def test_no_budget_caps_passes(tmp_path) -> None:
    sources, relationships, seeds = _fixture(tmp_path)
    plan = plan_subset(
        sources=sources,
        relationships=relationships,
        seeds=seeds,
        policy=FanOutPolicy(),
        job_seed=JOB_SEED,
        engine_version="test",
    )
    assert plan.budget_outcome == "pass"


def test_estimate_shape_with_zero_survivor_warning(tmp_path) -> None:
    customers_path = make_parquet(tmp_path, "customers", {"id": [1, 2, 3]})
    orders_path = make_parquet(tmp_path, "orders", {"id": [10, 11], "customer_id": [1, 2]})
    isolated_path = make_parquet(tmp_path, "isolated", {"id": [1]})
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
        "isolated": SubsetSource(path=isolated_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",)),)
    seeds = (SeedSpec(table="customers", mode="keys", key_columns=("id",), keys=((1,),)),)
    plan = plan_subset(
        sources=sources,
        relationships=relationships,
        seeds=seeds,
        policy=FanOutPolicy(),
        job_seed=JOB_SEED,
        engine_version="test",
    )
    by_table = {t.table: t for t in plan.tables}
    assert by_table["customers"].input_rows == 3
    assert by_table["customers"].seed_rows == 1
    assert by_table["customers"].surviving_rows == 1
    assert by_table["orders"].surviving_rows == 1
    assert by_table["isolated"].surviving_rows == 0
    assert any("isolated" in w for w in plan.warnings)
