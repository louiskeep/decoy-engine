"""SPRINT-1 Part A: each DuckDB connection `_runner.py` opens is capped by
its own PHASE-local liveness, not the run's single global-peak divisor.

Root cause this fixes: joiners for a table's incoming edges are co-live with
each other, but the SINK path always closes them (`_emit.py`'s
`on_stream_consumed`) before that table's own outgoing relation build opens --
so the build is the run's ONLY live instance at that moment, not the global
worst case. A fan-in-2 "hub" table (two parents feeding one child, which
itself has an outgoing edge) is the minimal shape that separates the two
phases: its joiners must divide the budget by 2, but its own relation build
must get the UNDIVIDED budget on the sink path, and budget // 3 (incoming + 1)
on the resident path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import _batch_join as batch_join_mod
from decoy_engine.execution.out_of_core import _relation as relation_mod
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

_SEED = b"\x00" * 8
_REG = get_default_registry()
_BUDGET_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB, arbitrary but divides evenly by 2 and 3


def _col(namespace: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _fan_in_hub_plan() -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("parent_a", TableSeed(per_column=(("id", _col("ns_a")),), per_group=())),
                ("parent_b", TableSeed(per_column=(("id", _col("ns_b")),), per_group=())),
                (
                    "hub",
                    TableSeed(
                        per_column=(
                            ("id", _col("ns_hub")),
                            ("a_id", _col("ns_a")),
                            ("b_id", _col("ns_b")),
                        ),
                        per_group=(),
                    ),
                ),
                ("leaf", TableSeed(per_column=(("hub_id", _col("ns_hub")),), per_group=())),
            ),
        )
    )


def _fan_in_hub_graph() -> RelationshipGraph:
    """parent_a, parent_b -> hub -> leaf. `hub` has 2 incoming edges (fan-in)
    AND 1 outgoing edge -- the minimal shape where a table's joiner phase and
    its own build phase have DIFFERENT live-instance counts."""
    return RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent_a",
                parent_columns=("id",),
                child_table="hub",
                child_columns=("a_id",),
                namespace="ns_a",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
            RelationshipEdge(
                parent_table="parent_b",
                parent_columns=("id",),
                child_table="hub",
                child_columns=("b_id",),
                namespace="ns_b",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
            RelationshipEdge(
                parent_table="hub",
                parent_columns=("id",),
                child_table="leaf",
                child_columns=("hub_id",),
                namespace="ns_hub",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )


def _fan_in_hub_sources() -> dict[str, pa.Table]:
    return {
        "parent_a": pa.table({"id": ["a1", "a2"]}),
        "parent_b": pa.table({"id": ["b1", "b2"]}),
        "hub": pa.table({"id": ["h1", "h2"], "a_id": ["a1", "a2"], "b_id": ["b1", "b2"]}),
        "leaf": pa.table({"hub_id": ["h1", "h2"]}),
    }


def _capture_connect_duckdb(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str | None]]:
    """Record the `memory_limit` every joiner-side and relation-build-side
    DuckDB connection opens with, while still opening the REAL connection so
    the run completes normally."""
    seen: dict[str, list[str | None]] = {"joiner": [], "relation": []}
    real_batch_join_connect = batch_join_mod.connect_duckdb
    real_relation_connect = relation_mod.connect_duckdb

    def fake_batch_join_connect(*, temp_dir: Any, memory_limit: str | None = None) -> Any:
        seen["joiner"].append(memory_limit)
        return real_batch_join_connect(temp_dir=temp_dir, memory_limit=memory_limit)

    def fake_relation_connect(*, temp_dir: Any, memory_limit: str | None = None) -> Any:
        seen["relation"].append(memory_limit)
        return real_relation_connect(temp_dir=temp_dir, memory_limit=memory_limit)

    monkeypatch.setattr(batch_join_mod, "connect_duckdb", fake_batch_join_connect)
    monkeypatch.setattr(relation_mod, "connect_duckdb", fake_relation_connect)
    return seen


class TestSinkPathPhaseCaps:
    def test_hub_joiners_divide_but_hub_build_gets_the_undivided_budget(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _capture_connect_duckdb(monkeypatch)
        run_fk_out_of_core(
            _fan_in_hub_plan(),
            _fan_in_hub_sources(),
            registry=_REG,
            relationship_graph=_fan_in_hub_graph(),
            sink=ParquetTransactionalSink(tmp_path / "out"),
            temp_dir=tmp_path / "work",
            budget_bytes=_BUDGET_BYTES,
        )
        divided = f"{_BUDGET_BYTES // 2 // (1024 * 1024)}MB"
        undivided = f"{_BUDGET_BYTES // (1024 * 1024)}MB"
        # hub's 2 incoming joiners (parent_a, parent_b) each divide by 2;
        # leaf's 1 incoming joiner (from hub) is alone, so it gets the full
        # budget too -- only fan-in > 1 actually divides.
        assert sorted(seen["joiner"]) == sorted([divided, divided, undivided])
        # Every relation build on the sink path (parent_a->hub, parent_b->hub,
        # hub->leaf) is the ONLY live instance at build time -- joiners always
        # close first (`_emit.py`'s on_stream_consumed) -- so ALL three get
        # the undivided budget, INCLUDING hub's own build despite hub's fan-in
        # of 2. This is the exact fix: the old global-peak divisor would have
        # starved this build to budget // 3.
        assert seen["relation"] == [undivided, undivided, undivided]

    def test_falls_back_to_flat_memory_limit_when_budget_bytes_is_none(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _capture_connect_duckdb(monkeypatch)
        run_fk_out_of_core(
            _fan_in_hub_plan(),
            _fan_in_hub_sources(),
            registry=_REG,
            relationship_graph=_fan_in_hub_graph(),
            sink=ParquetTransactionalSink(tmp_path / "out"),
            temp_dir=tmp_path / "work",
            memory_limit="777MB",
        )
        # No budget_bytes: every connection falls back to the same flat
        # string, exactly reproducing pre-Part-A behavior.
        assert seen["joiner"] == ["777MB", "777MB", "777MB"]
        assert seen["relation"] == ["777MB", "777MB", "777MB"]


class TestResidentPathPhaseCaps:
    def test_hub_build_divides_by_incoming_plus_one_without_a_sink(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _capture_connect_duckdb(monkeypatch)
        run_fk_out_of_core(
            _fan_in_hub_plan(),
            _fan_in_hub_sources(),
            registry=_REG,
            relationship_graph=_fan_in_hub_graph(),
            temp_dir=tmp_path / "work",
            budget_bytes=_BUDGET_BYTES,
        )
        divided_by_2 = f"{_BUDGET_BYTES // 2 // (1024 * 1024)}MB"
        divided_by_3 = f"{_BUDGET_BYTES // 3 // (1024 * 1024)}MB"
        undivided = f"{_BUDGET_BYTES // (1024 * 1024)}MB"
        assert sorted(seen["joiner"]) == sorted([divided_by_2, divided_by_2, undivided])
        # Resident path: hub's 2 joiners stay open through its own build, so
        # the build is live instance #3 (incoming + 1); leaf's build is
        # unaffected (leaf has 1 incoming, 0 outgoing -- no build at all).
        assert seen["relation"] == [undivided, undivided, divided_by_3]
