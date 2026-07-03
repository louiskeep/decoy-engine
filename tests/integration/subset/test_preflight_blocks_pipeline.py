"""Acceptance test 4 (second half): a failed preflight blocks SS2/SS3 entirely.

`run_subset_preflight` itself is exercised in `tests/unit/subset/test_preflight.py`
(SS1). This file proves the fail-closed contract end to end through the
orchestration layer: `plan_subset` raises `SubsetPreflightError` and
`compute_closure` never runs.
"""

from __future__ import annotations

import polars as pl
import pytest

from decoy_engine.subset import _api
from decoy_engine.subset._api import plan_subset
from decoy_engine.subset._errors import SubsetPreflightError
from decoy_engine.subset._types import FanOutPolicy, SeedSpec, SubsetSource
from tests.unit.subset.conftest import JOB_SEED, make_parquet, rel


def test_type_mismatch_blocks_plan_subset_and_ss3_never_runs(tmp_path, monkeypatch) -> None:
    customers_path = make_parquet(tmp_path, "customers", {"id": ["007", "008"]})
    orders_path = make_parquet(tmp_path, "orders", {"customer_id": [7, 8]})
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",)),)

    def _fail_if_called(*args, **kwargs):
        pytest.fail("SS3 compute_closure must not run when preflight fails")

    # Patch the binding `_api._compute` actually calls (it imported
    # compute_closure by name into its own module namespace), not the
    # `_closure` module -- patching `_closure.compute_closure` never touches
    # `_api.compute_closure`, which would make this guard a silent no-op.
    monkeypatch.setattr(_api, "compute_closure", _fail_if_called)

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


def test_direct_api_edgeless_csv_seed_table_fails_closed_not_a_raw_polars_error(tmp_path) -> None:
    """LOW-1 (dennis review): a direct `plan_subset(...)` caller with a seed
    table in no relationship, declared as a non-Parquet format, must get the
    clean `subset_requires_parquet` guidance -- not a raw polars error from
    `_keys.load_key_frames` unconditionally `scan_parquet`-ing a csv file.
    Config-driven runs were already safe via `config/_pipeline.py`; this is
    the direct-API gap.
    """
    csv_path = tmp_path / "standalone.csv"
    pl.DataFrame({"id": [1, 2]}).write_csv(csv_path)
    sources = {"standalone": SubsetSource(path=str(csv_path), format="csv")}

    with pytest.raises(SubsetPreflightError) as excinfo:
        plan_subset(
            sources=sources,
            relationships=(),
            seeds=(SeedSpec(table="standalone", mode="sample", key_columns=("id",), fraction=1.0),),
            policy=FanOutPolicy(),
            job_seed=JOB_SEED,
            engine_version="test",
        )
    assert excinfo.value.code == "subset_requires_parquet"
    assert "convert to Parquet for subsetting" in str(excinfo.value)
