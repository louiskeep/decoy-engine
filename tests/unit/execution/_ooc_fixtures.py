"""Single-edge fixtures shared by the streaming-join scaffolding tests.

Mirrors the plan/edge/relation construction `test_out_of_core_batch_join.py`
already uses for `ChildFkBatchJoiner` (the resident-parent joiner these tests
sit beside): a two-table plan, one `RelationshipEdge`, and a `ParentKeyRelation`
built with `build_parent_key_relation_from_tables`. Kept minimal for the P4-A.3
Task A smoke test (stage -> join -> resolve on a single edge); the byte-parity
fixtures (orphans, empty child, batch/run boundaries) are Task C's to add.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution.out_of_core._mask import mask_table
from decoy_engine.execution.out_of_core._relation import (
    ParentKeyRelation,
    build_parent_key_relation_from_tables,
)
from decoy_engine.execution.out_of_core._runner import _column_seed
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

_JOB_SEED = b"\x22" * 8


def _seed(strategy: str, *, namespace: str | None = None) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=(),
        coherent_with=(),
    )


def _plan(seed: ColumnSeed, *, parent: str, child: str, column: str) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                (parent, TableSeed(per_column=((column, seed),), per_group=())),
                (child, TableSeed(per_column=((column, seed),), per_group=())),
            ),
        )
    )


@dataclass
class OocEdgeFixture:
    """Everything one `StreamFkJoiner` needs, plus the plan for an oracle diff."""

    plan: Any
    edge: RelationshipEdge
    parent_relation: ParentKeyRelation
    child: pa.Table
    child_key_types: tuple[pa.DataType, ...]
    remap_seeds: tuple[ColumnSeed, ...] | None
    job_seed: bytes | None


def simple_edge_fixture(temp_dir: Path) -> OocEdgeFixture:
    """A plain FK edge: passthrough parent key, PRESERVE orphans, no REMAP.

    Every child key matches a parent row (`c1`/`c2`) except one deliberate
    orphan, so the smoke test exercises both the matched and PRESERVE-orphan
    branches of `resolve_batch` without needing REMAP's seed plumbing.
    """
    seed = _seed("passthrough")
    plan = _plan(seed, parent="parents", child="children", column="key")
    edge = RelationshipEdge(
        parent_table="parents",
        parent_columns=("key",),
        child_table="children",
        child_columns=("key",),
        namespace="parent_rel",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    parent = pa.table({"key": ["c1", "c2"]})
    child = pa.table(
        {
            "key": ["c1", "c2", "orphan1", "c1", "c2"],
            "amount": [1, 2, 3, 4, 5],
        }
    )
    masked_parent = mask_table(plan, edge.parent_table, parent, skip_columns=frozenset())
    relation = build_parent_key_relation_from_tables(
        source_parent=parent,
        masked_parent=masked_parent,
        edge=edge,
        temp_dir=temp_dir / "relation",
    )
    return OocEdgeFixture(
        plan=plan,
        edge=edge,
        parent_relation=relation,
        child=child,
        child_key_types=(child.column("key").type,),
        remap_seeds=None,
        job_seed=None,
    )


def remap_edge_fixture(temp_dir: Path) -> OocEdgeFixture:
    """A REMAP edge: the parent key is hash-masked, so a matched child row
    resolves to the parent's masked (hashed) value and an orphan is re-minted
    through the same strategy rather than preserving the raw source key.
    """
    seed = _seed("hash", namespace="cust_rel")
    plan = _plan(seed, parent="customers", child="orders", column="customer_id")
    edge = RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust_rel",
        orphan_policy=OrphanPolicy.REMAP,
    )
    parent = pa.table({"customer_id": ["c1", "c2"]})
    child = pa.table(
        {
            "customer_id": ["c1", "c2", "missing", "c1", "c2"],
            "amount": [10, 20, 30, 40, 50],
        }
    )
    masked_parent = mask_table(plan, edge.parent_table, parent, skip_columns=frozenset())
    relation = build_parent_key_relation_from_tables(
        source_parent=parent,
        masked_parent=masked_parent,
        edge=edge,
        temp_dir=temp_dir / "relation",
    )
    remap_seeds = tuple(_column_seed(plan, edge.parent_table, col) for col in edge.parent_columns)
    return OocEdgeFixture(
        plan=plan,
        edge=edge,
        parent_relation=relation,
        child=child,
        child_key_types=(child.column("customer_id").type,),
        remap_seeds=remap_seeds,
        job_seed=plan.seed_envelope.job_seed,
    )


__all__ = ["OocEdgeFixture", "remap_edge_fixture", "simple_edge_fixture"]
