"""Acceptance test 4 (second half): a failed preflight blocks SS2/SS3 entirely.

`run_subset_preflight` itself is exercised in `tests/unit/subset/test_preflight.py`
(SS1). This file proves the fail-closed contract end to end through the
orchestration layer: `plan_subset` raises `SubsetPreflightError` and
`compute_closure` never runs.
"""

from __future__ import annotations

import pytest

from decoy_engine.subset import _closure
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
