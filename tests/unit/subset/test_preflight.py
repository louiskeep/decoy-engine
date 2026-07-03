"""SS1 preflight tests. Includes acceptance test 4 (fail-closed preflight)."""

from __future__ import annotations

import pytest

from decoy_engine.subset import _closure
from decoy_engine.subset._api import plan_subset
from decoy_engine.subset._edges import relationships_from_config
from decoy_engine.subset._errors import SubsetPreflightError
from decoy_engine.subset._preflight import run_subset_preflight
from decoy_engine.subset._types import FanOutPolicy, SeedSpec, SubsetSource
from tests.unit.subset.conftest import JOB_SEED, make_parquet, rel


def test_type_mismatch_fails_closed_and_blocks_ss3(tmp_path, monkeypatch) -> None:
    customers_path = make_parquet(tmp_path, "customers", {"id": ["007", "008"]})
    orders_path = make_parquet(tmp_path, "orders", {"customer_id": [7, 8]})
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",)),)

    report = run_subset_preflight(sources=sources, relationships=relationships)
    assert report.passed is False
    assert len(report.failures) == 1
    failure = report.failures[0]
    assert failure.code == "subset_relationship_type_mismatch"
    assert failure.relationship == "customers.id -> orders.customer_id"
    assert "String" in failure.message
    assert "Int64" in failure.message

    def _fail_if_called(*args, **kwargs):
        pytest.fail("SS3 compute_closure must not run when preflight fails")

    monkeypatch.setattr(_closure, "compute_closure", _fail_if_called)

    with pytest.raises(SubsetPreflightError) as excinfo:
        plan_subset(
            sources=sources,
            relationships=relationships,
            seeds=(SeedSpec(table="customers", mode="sample", key_columns=("id",), fraction=1.0),),
            policy=FanOutPolicy(),
            job_seed=JOB_SEED,
            engine_version="test",
        )
    assert excinfo.value.code == "subset_relationship_type_mismatch"


def test_half_declared_composite_fails_via_relationships_from_config() -> None:
    config = {
        "relationships": [
            {
                "parent": {"table": "a", "columns": ["k1", "k2"]},
                "children": [{"table": "b", "columns": ["k1"]}],
                "orphan_policy": "preserve",
            }
        ]
    }
    with pytest.raises(SubsetPreflightError) as excinfo:
        relationships_from_config(config)
    assert excinfo.value.code == "subset_relationship_composite_length"
    failure = excinfo.value.report.failures[0]
    assert "2" in failure.message
    assert "1" in failure.message


def test_dangling_column_fails_closed(tmp_path) -> None:
    customers_path = make_parquet(tmp_path, "customers", {"id": [1, 2]})
    orders_path = make_parquet(tmp_path, "orders", {"other_col": [1, 2]})
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_ref",)),)
    report = run_subset_preflight(sources=sources, relationships=relationships)
    assert report.passed is False
    failure = report.failures[0]
    assert failure.code == "subset_relationship_column_missing"
    assert "customer_ref" in failure.message
    assert "orders" in failure.message
    assert "other_col" in failure.message


def test_int32_parent_int64_child_compat_passes(tmp_path) -> None:
    customers_path = make_parquet(
        tmp_path, "customers", {"id": [1, 2]}, schema={"id": __import__("polars").Int32}
    )
    orders_path = make_parquet(tmp_path, "orders", {"customer_id": [1, 2]})
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",), policy="preserve"),)
    report = run_subset_preflight(sources=sources, relationships=relationships)
    assert report.passed is True


def test_string_string_compat_passes(tmp_path) -> None:
    customers_path = make_parquet(tmp_path, "customers", {"id": ["a", "b"]})
    orders_path = make_parquet(tmp_path, "orders", {"customer_id": ["a", "b"]})
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",)),)
    report = run_subset_preflight(sources=sources, relationships=relationships)
    assert report.passed is True


def test_float_key_rejected(tmp_path) -> None:
    import polars as pl

    customers_path = make_parquet(
        tmp_path, "customers", {"id": [1.0, 2.0]}, schema={"id": pl.Float64}
    )
    orders_path = make_parquet(
        tmp_path, "orders", {"customer_id": [1.0, 2.0]}, schema={"customer_id": pl.Float64}
    )
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",)),)
    report = run_subset_preflight(sources=sources, relationships=relationships)
    assert report.passed is False
    assert report.failures[0].code == "subset_relationship_key_float_unsupported"


def test_csv_source_rejected_with_guidance(tmp_path) -> None:
    orders_path = make_parquet(tmp_path, "orders", {"customer_id": [1, 2]})
    sources = {
        "customers": SubsetSource(path=str(tmp_path / "customers.csv"), format="csv"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",)),)
    report = run_subset_preflight(sources=sources, relationships=relationships)
    assert report.passed is False
    assert report.failures[0].code == "subset_requires_parquet"
    assert "convert to Parquet for subsetting" in report.failures[0].message


@pytest.mark.parametrize(
    ("policy", "expect_passed", "expect_warning"),
    [
        ("fail", False, False),
        ("warn", True, True),
        ("preserve", True, False),
    ],
)
def test_orphan_prescan_matches_fk_validity_semantics(
    tmp_path, policy, expect_passed, expect_warning
) -> None:
    customers_path = make_parquet(tmp_path, "customers", {"id": [1, 2]})
    orders_path = make_parquet(tmp_path, "orders", {"customer_id": [1, 1, 3, None]})
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",), policy=policy),)
    report = run_subset_preflight(sources=sources, relationships=relationships)
    assert report.passed is expect_passed
    # Source-orphan failures (unlike schema-level 5.0-5.3 failures) still populate
    # `edges`: 5.4 runs and reports per-edge counts regardless of orphan_policy.
    edge_report = report.edges[0]
    assert edge_report.child_row_count == 4
    assert edge_report.non_null_child_key_count == 3
    assert edge_report.parent_match_count == 2
    assert edge_report.source_orphan_count == 1
    if policy == "fail":
        assert any(f.code == "subset_source_orphans" for f in report.failures)
    if expect_warning:
        assert len(report.warnings) == 1
