"""Single-edge fixtures shared by the streaming-join scaffolding tests.

Mirrors the plan/edge/relation construction `test_out_of_core_batch_join.py`
already uses for `ChildFkBatchJoiner` (the resident-parent joiner these tests
sit beside): a two-table plan, one `RelationshipEdge`, and a `ParentKeyRelation`
built with `build_parent_key_relation_from_tables`. `simple_edge_fixture` /
`remap_edge_fixture` back the P4-A.3 Task A smoke test (stage -> join ->
resolve on a single edge); `orphan_and_null_edge_fixture`,
`empty_child_edge_fixture`, and `wide_edge_fixture` are Task C's byte-parity
fixtures (orphans, empty child, batch/run boundaries).
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


def orphan_and_null_edge_fixture(temp_dir: Path) -> OocEdgeFixture:
    """A WARN edge whose child mixes matched rows, real orphans (unmatched
    non-null keys), and null FK rows (never orphans, under any policy).
    Exercises `mask_child_fk`'s WARN aggregation and the reorder path's
    `resolve_batch` warning parity together (P4-A.3 acceptance test #3b).
    """
    seed = _seed("passthrough")
    plan = _plan(seed, parent="parents", child="children", column="key")
    edge = RelationshipEdge(
        parent_table="parents",
        parent_columns=("key",),
        child_table="children",
        child_columns=("key",),
        namespace="parent_rel",
        orphan_policy=OrphanPolicy.WARN,
    )
    parent = pa.table({"key": ["p0", "p1", "p2"]})
    child = pa.table(
        {
            "key": ["p0", "orphanA", None, "p1", "orphanB", "p2", None, "p0"],
            "amount": [1, 2, 3, 4, 5, 6, 7, 8],
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


def empty_child_edge_fixture(temp_dir: Path) -> OocEdgeFixture:
    """A zero-row child against a real (non-empty) parent relation: the
    reorder path's `N=0` case (P4-A.3 acceptance test #3b), which must yield
    an empty, correctly-typed result and never crash on an empty stage.
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
            "key": pa.array([], type=pa.string()),
            "amount": pa.array([], type=pa.int64()),
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


def wide_edge_fixture(
    temp_dir: Path, *, parent_rows: int = 40, child_rows: int = 400
) -> OocEdgeFixture:
    """A larger PRESERVE edge sized so a small `batch_rows` spans several
    join-output batches AND a small `run_bytes_cap` spills the bounded sorter
    across several runs -- the batch/run-boundary case (P4-A.3 acceptance
    test #3b), where a staging/resolution/merge boundary bug would surface.
    Roughly a third of the child rows are deliberate orphans, one in six is a
    null FK, and the rest cycle through every parent row at least once.
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
    parent = pa.table({"key": [f"p{i}" for i in range(parent_rows)]})
    child_keys: list[str | None] = []
    for i in range(child_rows):
        if i % 6 == 0:
            child_keys.append(None)
        elif i % 3 == 0:
            child_keys.append(f"orphan{i}")
        else:
            child_keys.append(f"p{i % parent_rows}")
    child = pa.table(
        {
            "key": child_keys,
            "amount": list(range(child_rows)),
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


__all__ = [
    "OocEdgeFixture",
    "empty_child_edge_fixture",
    "orphan_and_null_edge_fixture",
    "remap_edge_fixture",
    "simple_edge_fixture",
    "wide_edge_fixture",
]
