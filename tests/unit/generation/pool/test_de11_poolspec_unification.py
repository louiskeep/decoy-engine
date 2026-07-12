"""DE-11 regression: PoolSpec unification.

Before this fix the pool-capacity contract was checked against two DIFFERENT
quantities that could not agree, and the operator-declared pool_size never
reached the sampler:

- compile (`_validate`) checked UNIQUE feasibility against the SOURCE distinct
  count, so a 500-row / 50-distinct / pool_size=200 job COMPILED;
- the top-level `pool_size` was not carried into the faker handler, which
  silently fell back to a 10_000-value default pool, so the same job then
  produced 500 distinct outputs from an undeclared pool.

The fix carries ONE typed `PoolSpec` in the plan, sizes both the compile check
and the runtime sampler from ONE shared capacity function keyed on the NON-NULL
OUTPUT ROW COUNT, and fails a too-small declared pool CLOSED.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionResult, PandasExecutionAdapter
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._faker import FakerStrategyHandler
from decoy_engine.generation.pool import GenerationError, PoolCapacityError
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.plan import PlanCompileError, compile_plan  # noqa: F401
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.profile import ColumnProfile, Profile, Relationship, TableProfile
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0x0123456789).to_bytes(8, "big")


def _profile(*, row_count: int, null_count: int, distinct: int) -> Profile:
    """Single 'customers.email' column with explicit row/null/distinct counts,
    plus the customer_id PK + orders FK that make the config compile."""
    email = ColumnProfile(
        name="email",
        dtype="object",
        row_count=row_count,
        null_count=null_count,
        distinct_count=distinct,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )
    customer_id = ColumnProfile(
        name="customer_id",
        dtype="object",
        row_count=row_count,
        null_count=0,
        distinct_count=row_count,
        sampled=False,
        is_candidate_key_sampled=True,
        declared_pk=True,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )
    return Profile(
        schema_version=1,
        tables=(
            TableProfile(name="customers", row_count=row_count, columns=(customer_id, email)),
            TableProfile(
                name="orders",
                row_count=row_count,
                columns=(
                    ColumnProfile(
                        name="customer_id",
                        dtype="object",
                        row_count=row_count,
                        null_count=0,
                        distinct_count=row_count,
                        sampled=False,
                        is_candidate_key_sampled=False,
                        declared_pk=False,
                        is_fk=True,
                        fk_target=("customers", "customer_id"),
                        pii_class=None,
                    ),
                ),
            ),
        ),
        relationships=(
            Relationship(
                parent_table="customers",
                parent_columns=("customer_id",),
                child_table="orders",
                child_columns=("customer_id",),
                namespace="customer_identity",
            ),
        ),
        profiled_at=datetime(2026, 5, 27, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _config(cardinality: str, *, top_pool: int | None = None, pc_pool: int | None = None) -> dict:
    col: dict[str, Any] = {
        "name": "email",
        "strategy": "faker",
        "provider": "person_email",
        "cardinality_mode": cardinality,
    }
    if top_pool is not None:
        col["pool_size"] = top_pool
    if pc_pool is not None:
        col["provider_config"] = {"pool_size": pc_pool}
    return {
        "global_settings": {"seed": 1, "on_pool_exhaustion": "fail"},
        "tables": [{"name": "customers", "columns": [col]}],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["customer_id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "fail",
                "namespace": "customer_identity",
            }
        ],
    }


def _email_seed(plan: Any) -> ColumnSeed:
    per_table = dict(plan.seed_envelope.per_table)
    return dict(per_table["customers"].per_column)["email"]


# --------------------------------------------------------------------------
# Capacity checks agree, keyed on non-null output rows (not source distinct)
# --------------------------------------------------------------------------


class TestCompileCapacityKeyedOnOutputRows:
    def test_unique_pool_between_distinct_and_rows_now_fails_closed(self) -> None:
        """THE DE-11 repro: 500 rows, 50 distinct, pool_size=200. Pre-fix this
        compiled (200 >= 50 source distinct); post-fix it fails closed because
        UNIQUE needs one distinct value per OUTPUT ROW (500 > 200)."""
        with pytest.raises(PoolCapacityError) as exc:
            compile_plan(
                _config("unique", top_pool=200),
                _profile(row_count=500, null_count=0, distinct=50),
                decoy_engine_version="0.1.0",
            )
        assert exc.value.code == "pool_too_small_for_source"

    def test_unique_feasible_when_pool_ge_output_rows(self) -> None:
        plan = compile_plan(
            _config("unique", top_pool=500),
            _profile(row_count=500, null_count=0, distinct=50),
            decoy_engine_version="0.1.0",
        )
        assert plan is not None

    def test_unique_keyed_on_nonnull_rows_passes(self) -> None:
        """400 of 500 rows are null -> only 100 distinct outputs needed;
        pool_size=150 is enough."""
        plan = compile_plan(
            _config("unique", top_pool=150),
            _profile(row_count=500, null_count=400, distinct=50),
            decoy_engine_version="0.1.0",
        )
        assert plan is not None

    def test_unique_keyed_on_nonnull_rows_fails(self) -> None:
        """Same 100 non-null rows, pool_size=90 < 100 -> fails closed."""
        with pytest.raises(PoolCapacityError) as exc:
            compile_plan(
                _config("unique", top_pool=90),
                _profile(row_count=500, null_count=400, distinct=50),
                decoy_engine_version="0.1.0",
            )
        assert exc.value.code == "pool_too_small_for_source"


# --------------------------------------------------------------------------
# Canonical config field -> PoolSpec -> plan
# --------------------------------------------------------------------------


class TestPoolSpecCarriedInPlan:
    def test_top_level_pool_size_is_canonical(self) -> None:
        plan = compile_plan(
            _config("reuse", top_pool=222),
            _profile(row_count=50, null_count=0, distinct=50),
            decoy_engine_version="0.1.0",
        )
        spec = _email_seed(plan).pool_spec
        assert spec is not None
        assert spec.pool_size == 222
        assert spec.size_source == "declared"
        assert spec.unique is False

    def test_provider_config_pool_size_is_the_fallback(self) -> None:
        plan = compile_plan(
            _config("reuse", pc_pool=77),
            _profile(row_count=50, null_count=0, distinct=50),
            decoy_engine_version="0.1.0",
        )
        spec = _email_seed(plan).pool_spec
        assert spec is not None
        assert spec.pool_size == 77
        assert spec.size_source == "declared"

    def test_top_level_wins_over_provider_config(self) -> None:
        plan = compile_plan(
            _config("reuse", top_pool=222, pc_pool=77),
            _profile(row_count=50, null_count=0, distinct=50),
            decoy_engine_version="0.1.0",
        )
        assert _email_seed(plan).pool_spec.pool_size == 222

    def test_default_pool_size_marked_default(self) -> None:
        plan = compile_plan(
            _config("reuse"),
            _profile(row_count=50, null_count=0, distinct=50),
            decoy_engine_version="0.1.0",
        )
        spec = _email_seed(plan).pool_spec
        assert spec is not None
        assert spec.size_source == "default"
        assert spec.pool_size == 10_000


# --------------------------------------------------------------------------
# Config -> handler boundary: the declared pool_size reaches the sampler
# --------------------------------------------------------------------------


def _run(plan: Any, table: pa.Table) -> ExecutionResult:
    return PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )


def _plan_with_spec(cs: ColumnSeed) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("people", TableSeed(per_column=(("email", cs),), per_group=())),),
        )
    )


class TestDeclaredPoolReachesHandler:
    def test_declared_pool_size_bounds_distinct_outputs(self) -> None:
        """A tiny declared pool (size 2) in reuse mode over 50 rows must yield
        at most 2 distinct outputs. Pre-fix the handler ignored the declared
        size and drew from the 10_000 default, producing many distinct
        values."""
        from decoy_engine.plan._types import PoolSpec

        cs = ColumnSeed(
            namespace=None,
            strategy="faker",
            provider="person_email",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(),
            coherent_with=(),
            pool_spec=PoolSpec(pool_size=2, unique=False, size_source="declared"),
        )
        src = pa.table({"email": [f"u{i}" for i in range(50)]})
        out = _run(_plan_with_spec(cs), src).output.column("email").to_pylist()
        assert len(set(out)) <= 2

    def test_too_small_declared_unique_pool_fails_closed(self) -> None:
        """UNIQUE over 8 non-null rows with a declared pool of 3 must raise the
        typed pool-family error (fail closed), not silently draw 8 distinct
        from a bigger undeclared pool."""
        from decoy_engine.plan._types import PoolSpec

        cs = ColumnSeed(
            namespace=None,
            strategy="faker",
            provider="person_email",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="unique",
            deterministic=False,
            provider_config=(),
            coherent_with=(),
            pool_spec=PoolSpec(pool_size=3, unique=True, size_source="declared"),
        )
        import pandas as pd

        df = pd.DataFrame({"email": [f"u{i}" for i in range(8)]})
        ctx = StrategyContext(
            registry=_REG,
            pool_cache=PoolCache(),
            relationship_graph=_GRAPH,
            namespace_registry=_NS,
            job_seed=_SEED,
        )
        with pytest.raises(GenerationError) as exc:
            FakerStrategyHandler().run(df, "email", cs, ctx)
        assert exc.value.code == "uniqueness_impossible"
