"""engine-v2 S9 slice 2c: composite routing through the execution adapter.

A composite WorkNode writes all output columns in one generate_bundle pass; the
adapter resolves the generator via the factory + the whole-tuple namespace. Tests
the S8<->S9 integration: coherence (email local-part == masked first.last;
city/state/zip in the locality table) + reproducibility.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, ExecutionResult, PandasExecutionAdapter
from decoy_engine.generation.composite import load_locality_table
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceBinding, NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_SEED = (0xABCDEF).to_bytes(8, "big")


def _col(provider: str, coherent_with: tuple[str, ...]) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="<composite>",
        provider=provider,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=coherent_with,
    )


def _ns_registry(table: str, columns: tuple[str, ...], namespace: str) -> NamespaceRegistry:
    group = tuple(sorted(columns))
    return NamespaceRegistry(
        bindings=(NamespaceBinding(namespace=namespace, declared_by=((table, group),)),)
    )


def _plan(table: str, per_column: tuple[tuple[str, ColumnSeed], ...]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=((table, TableSeed(per_column=per_column, per_group=())),),
        )
    )


def _run(plan: Any, table: pa.Table, ns_registry: NamespaceRegistry) -> ExecutionResult:
    return PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=ns_registry
    )


def _name_email_setup() -> tuple[Any, NamespaceRegistry]:
    cols = ("email", "first_name", "last_name")
    per_column = (
        ("first_name", _col("composite_name_email", ("last_name", "email"))),
        ("last_name", _col("composite_name_email", ("first_name", "email"))),
        ("email", _col("composite_name_email", ("first_name", "last_name"))),
    )
    plan = _plan("people", per_column)
    ns = _ns_registry("people", cols, "ne_ns")
    return plan, ns


class TestCompositeNameEmailRouting:
    def test_email_coherent_with_masked_name(self) -> None:
        src = pa.table(
            {"first_name": ["X", "Y"], "last_name": ["P", "Q"], "email": ["a@b.com", "c@d.com"]}
        )
        plan, ns = _name_email_setup()
        out = _run(plan, src, ns).output.to_pydict()
        for i in range(2):
            first = str(out["first_name"][i]).lower()
            last = str(out["last_name"][i]).lower()
            assert str(out["email"][i]).startswith(f"{first}.{last}@")

    def test_reproducible_across_runs(self) -> None:
        src = pa.table(
            {"first_name": ["X", "Y"], "last_name": ["P", "Q"], "email": ["a@b.com", "c@d.com"]}
        )
        plan, ns = _name_email_setup()
        out1 = _run(plan, src, ns).output.to_pydict()
        out2 = _run(plan, src, ns).output.to_pydict()
        assert out1 == out2


class TestCompositeCityStateZipRouting:
    def test_triples_in_locality_table(self) -> None:
        table_set = set(load_locality_table())
        cols = ("city", "state", "zip")
        per_column = (
            ("city", _col("composite_city_state_zip", ("state", "zip"))),
            ("state", _col("composite_city_state_zip", ("city", "zip"))),
            ("zip", _col("composite_city_state_zip", ("city", "state"))),
        )
        plan = _plan("locations", per_column)
        ns = _ns_registry("locations", cols, "loc_ns")
        src = pa.table({"city": ["Old", "Town"], "state": ["AA", "BB"], "zip": ["00000", "11111"]})
        out = _run(plan, src, ns).output.to_pydict()
        triples = list(zip(out["city"], out["state"], out["zip"], strict=True))
        assert all(t in table_set for t in triples)


class TestCompositeOutputColumnMissing:
    def test_missing_output_column_raises(self) -> None:
        # M1: a composite whose bundle includes a column absent from the source
        # frame must raise, not silently drop it. Source omits last_name.
        plan, ns = _name_email_setup()
        src = pa.table({"first_name": ["X"], "email": ["a@b.com"]})
        with pytest.raises(ExecutionError) as exc:
            _run(plan, src, ns)
        assert exc.value.code == "composite_output_column_missing"
