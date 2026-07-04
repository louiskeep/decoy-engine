"""Acceptance tests 1 (referential completeness) and 5 (dry-run == materialized)."""

from __future__ import annotations

import polars as pl

from decoy_engine.subset._api import plan_subset, run_subset
from decoy_engine.subset._types import FanOutPolicy, SeedSpec, SubsetSource
from tests.unit.subset.conftest import JOB_SEED, make_parquet, rel


def _relational_fixture(tmp_path):
    customers_path = make_parquet(tmp_path, "customers", {"id": list(range(1, 101))})
    orders_path = make_parquet(
        tmp_path,
        "orders",
        {"id": list(range(1, 201)), "customer_id": [(i % 100) + 1 for i in range(200)]},
    )
    items_path = make_parquet(
        tmp_path,
        "order_items",
        {"id": list(range(1, 401)), "order_id": [(i % 200) + 1 for i in range(400)]},
    )
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
        "order_items": SubsetSource(path=items_path, format="parquet"),
    }
    relationships = (
        rel("customers", ("id",), "orders", ("customer_id",), policy="preserve"),
        rel("orders", ("id",), "order_items", ("order_id",), policy="preserve"),
    )
    seeds = (SeedSpec(table="customers", mode="sample", key_columns=("id",), fraction=0.02),)
    return sources, relationships, seeds


def test_referential_completeness_and_exact_downward_pull(tmp_path) -> None:
    sources, relationships, seeds = _relational_fixture(tmp_path)
    out_dir = tmp_path / "out"
    result = run_subset(
        sources=sources,
        relationships=relationships,
        seeds=seeds,
        policy=FanOutPolicy(),
        job_seed=JOB_SEED,
        engine_version="test",
        output_dir=out_dir,
    )

    customers_out = pl.read_parquet(out_dir / "customers.parquet")
    orders_out = pl.read_parquet(out_dir / "orders.parquet")
    items_out = pl.read_parquet(out_dir / "order_items.parquet")

    assert customers_out.height == 2

    # No orphans.
    assert set(orders_out["customer_id"].to_list()) <= set(customers_out["id"].to_list())
    assert set(items_out["order_id"].to_list()) <= set(orders_out["id"].to_list())

    # Downward completeness: exact equality (catches over-pull AND under-pull).
    full_orders = pl.read_parquet(sources["orders"].path)
    expected_orders = full_orders.filter(pl.col("customer_id").is_in(customers_out["id"].to_list()))
    assert orders_out.height == expected_orders.height
    assert set(orders_out["id"].to_list()) == set(expected_orders["id"].to_list())

    full_items = pl.read_parquet(sources["order_items"].path)
    expected_items = full_items.filter(pl.col("order_id").is_in(orders_out["id"].to_list()))
    assert items_out.height == expected_items.height
    assert set(items_out["id"].to_list()) == set(expected_items["id"].to_list())

    assert set(result.output_paths) == {
        ("customers", str(out_dir / "customers.parquet")),
        ("orders", str(out_dir / "orders.parquet")),
        ("order_items", str(out_dir / "order_items.parquet")),
    }


def test_dry_run_equals_materialized_exact(tmp_path) -> None:
    sources, relationships, seeds = _relational_fixture(tmp_path)
    out_dir = tmp_path / "out"
    plan = plan_subset(
        sources=sources,
        relationships=relationships,
        seeds=seeds,
        policy=FanOutPolicy(),
        job_seed=JOB_SEED,
        engine_version="test",
    )
    result = run_subset(
        sources=sources,
        relationships=relationships,
        seeds=seeds,
        policy=FanOutPolicy(),
        job_seed=JOB_SEED,
        engine_version="test",
        output_dir=out_dir,
    )
    plan_by_table = {t.table: t.surviving_rows for t in plan.tables}
    manifest_by_table = {t.table: t.surviving_rows for t in result.manifest.tables}
    for table, path in result.output_paths:
        materialized_height = pl.read_parquet(path).height
        assert plan_by_table[table] == materialized_height == manifest_by_table[table]


def test_zero_survivor_table_writes_schema_preserving_empty_parquet(tmp_path) -> None:
    customers_path = make_parquet(tmp_path, "customers", {"id": [1, 2, 3]})
    orders_path = make_parquet(tmp_path, "orders", {"id": [10], "customer_id": [99]})
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",), policy="preserve"),)
    seeds = (SeedSpec(table="customers", mode="keys", key_columns=("id",), keys=((1,),)),)
    out_dir = tmp_path / "out"
    run_subset(
        sources=sources,
        relationships=relationships,
        seeds=seeds,
        policy=FanOutPolicy(),
        job_seed=JOB_SEED,
        engine_version="test",
        output_dir=out_dir,
    )
    orders_out = pl.read_parquet(out_dir / "orders.parquet")
    assert orders_out.height == 0
    assert orders_out.schema == pl.read_parquet(orders_path).schema


def test_existing_nonempty_output_dir_rejected(tmp_path) -> None:
    from decoy_engine.subset._errors import SubsetConfigError

    sources, relationships, seeds = _relational_fixture(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "stray.txt").write_text("x")
    import pytest

    with pytest.raises(SubsetConfigError) as excinfo:
        run_subset(
            sources=sources,
            relationships=relationships,
            seeds=seeds,
            policy=FanOutPolicy(),
            job_seed=JOB_SEED,
            engine_version="test",
            output_dir=out_dir,
        )
    assert excinfo.value.code == "subset_output_dir_exists"
