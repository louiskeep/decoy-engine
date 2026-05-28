"""engine-v2 S9 slice 2f: date_shift (derive offset) + formula (V1 safe-eval)."""

from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, ExecutionResult, PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0x99).to_bytes(8, "big")


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    deterministic: bool = False,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(col_name: str, seed: ColumnSeed) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("t", TableSeed(per_column=((col_name, seed),), per_group=())),),
        )
    )


def _run(plan: Any, table: pa.Table) -> ExecutionResult:
    return PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )


class TestDateShift:
    def test_shifts_within_range_reproducible_null_preserved(self) -> None:
        src = pa.table({"d": ["2020-01-15", "2020-06-30", None, "2020-01-15"]})
        seed = _col(
            "date_shift",
            namespace="dates",
            deterministic=True,
            provider_config=(("min_days", -10), ("max_days", 10), ("date_format", "%Y-%m-%d")),
        )
        out1 = _run(_plan("d", seed), src).output.column("d").to_pylist()
        out2 = _run(_plan("d", seed), src).output.column("d").to_pylist()
        assert out1 == out2  # reproducible
        assert out1[2] is None  # null preserved
        assert out1[0] == out1[3]  # same source date -> same shift
        shifted = datetime.datetime.strptime(out1[0], "%Y-%m-%d").date()
        assert abs((shifted - datetime.date(2020, 1, 15)).days) <= 10

    def test_requires_namespace(self) -> None:
        src = pa.table({"d": ["2020-01-15"]})
        seed = _col(
            "date_shift",
            namespace=None,
            deterministic=True,
            provider_config=(("date_format", "%Y-%m-%d"),),
        )
        with pytest.raises(ExecutionError) as exc:
            _run(_plan("d", seed), src)
        assert exc.value.code == "date_shift_requires_namespace"


class TestFormula:
    def test_applies_expression_preserves_null(self) -> None:
        src = pa.table({"n": [1, 2, None]})
        seed = _col("formula", provider_config=(("formula", "value * 2"),))
        out = _run(_plan("n", seed), src).output.column("n").to_pylist()
        assert float(out[0]) == 2.0
        assert float(out[1]) == 4.0
        assert out[2] is None
