"""Acceptance test 6: manifest completeness + NO raw key values (sentinel leak test)."""

from __future__ import annotations

import json

from decoy_engine.subset._api import run_subset
from decoy_engine.subset._types import FanOutPolicy, SeedSpec, SubsetSource
from tests.unit.subset.conftest import JOB_SEED, make_parquet, rel

SENTINEL = "SENTINEL_KEY_93217"


def test_manifest_has_no_raw_key_values_and_is_complete(tmp_path) -> None:
    customers_path = make_parquet(tmp_path, "customers", {"id": [SENTINEL, "other-1", "other-2"]})
    orders_path = make_parquet(
        tmp_path, "orders", {"id": [1, 2], "customer_id": [SENTINEL, "other-1"]}
    )
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",), policy="preserve"),)
    seeds = (SeedSpec(table="customers", mode="keys", key_columns=("id",), keys=((SENTINEL,),)),)
    out_dir = tmp_path / "out"

    result = run_subset(
        sources=sources,
        relationships=relationships,
        seeds=seeds,
        policy=FanOutPolicy(),
        job_seed=JOB_SEED,
        engine_version="test-engine-version",
        output_dir=out_dir,
    )

    manifest_path = out_dir / "subset-manifest.json"
    raw_text = manifest_path.read_text()
    assert "SENTINEL" not in raw_text
    assert SENTINEL not in raw_text

    data = json.loads(raw_text)

    assert data["seed_specs_public"][0] == {
        "table": "customers",
        "mode": "keys",
        "key_columns": ["id"],
        "key_count": 1,
    }

    tables_by_name = {t["table"]: t for t in data["tables"]}
    for table in ("customers", "orders"):
        assert "input_rows" in tables_by_name[table]
        assert "surviving_rows" in tables_by_name[table]
        assert "seed_rows" in tables_by_name[table]

    edge = data["edges"][0]
    for key in ("edge_id", "direction", "rows_added_downward", "rows_added_upward"):
        assert key in edge

    assert isinstance(data["closure_rounds"], int)
    assert "max_total_rows" in data["budget"]
    assert "max_table_seed_multiple" in data["budget"]
    assert data["budget_outcome"] == "pass"
    assert data["engine_version"] == "test-engine-version"

    assert data["preflight_summary"]
    pf = data["preflight_summary"][0]
    for key in (
        "relationship",
        "namespace",
        "orphan_policy",
        "child_row_count",
        "non_null_child_key_count",
        "parent_match_count",
        "source_orphan_count",
        "invalid_count",
    ):
        assert key in pf

    # Result object mirrors the manifest.
    assert result.manifest.manifest_version == 1
