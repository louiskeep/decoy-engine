"""Nice-to-have (dennis review): `verify_closure` must run in `plan_subset` too.

The dry-run/estimate is exactly the surface an operator inspects to trust a
"2% subset" before committing to `run_subset`, so it should carry the same
independent no-orphan invariant guard, not just the materializing path.
"""

from __future__ import annotations

import pytest

from decoy_engine.subset import _api
from decoy_engine.subset._api import plan_subset
from decoy_engine.subset._closure import ClosureResult
from decoy_engine.subset._errors import SubsetInternalError
from decoy_engine.subset._types import FanOutPolicy, SeedSpec, SubsetSource
from tests.unit.subset.conftest import JOB_SEED, make_parquet, rel


def test_plan_subset_raises_on_corrupted_closure(tmp_path, monkeypatch) -> None:
    customers_path = make_parquet(tmp_path, "customers", {"id": [1, 2]})
    orders_path = make_parquet(tmp_path, "orders", {"id": [10, 11], "customer_id": [1, 2]})
    sources = {
        "customers": SubsetSource(path=customers_path, format="parquet"),
        "orders": SubsetSource(path=orders_path, format="parquet"),
    }
    relationships = (rel("customers", ("id",), "orders", ("customer_id",), policy="preserve"),)
    # Seed the CHILD table so the closure's upward pull is what populates
    # `customers`; corrupting that pull below is what the invariant must catch.
    seeds = (SeedSpec(table="orders", mode="sample", key_columns=("id",), fraction=1.0),)

    real_compute_closure = _api.compute_closure

    def _corrupted_compute_closure(**kwargs: object) -> ClosureResult:
        result = real_compute_closure(**kwargs)  # type: ignore[arg-type]
        # Simulate a closure engine bug: pretend the upward pull into
        # `customers` never happened, even though `orders` (seeded) still
        # references both customer keys and both exist in the source parent
        # table. This is exactly the dangling-FK failure mode the invariant
        # guard exists to catch.
        survivors = dict(result.survivors)
        survivors["customers"] = frozenset()
        return ClosureResult(
            survivors=survivors,
            rounds=result.rounds,
            terminated_by=result.terminated_by,
            edge_stats=result.edge_stats,
            trace=result.trace,
        )

    monkeypatch.setattr(_api, "compute_closure", _corrupted_compute_closure)

    with pytest.raises(SubsetInternalError) as excinfo:
        plan_subset(
            sources=sources,
            relationships=relationships,
            seeds=seeds,
            policy=FanOutPolicy(),
            job_seed=JOB_SEED,
            engine_version="test",
        )
    assert excinfo.value.code == "subset_closure_invariant_violated"
