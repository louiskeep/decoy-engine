"""Multi-table FK relational fixture for memory-scaling measurement.

The committed perf tiers (`schema.py`) are a single flat table, so they never
exercise the `relationships` full-frame path that the engine takes when a config
declares foreign keys (chunked/streaming is rejected; every table loads full-width
at once). That path is the one that OOMs at scale, and it is the subject of
`docs/relationships-memory-scaling.md`. This module builds the missing shape: a
parent to child to grandchild chain, wide payload columns, and a controllable
orphan fraction, sized by row count so the real memory wall can be measured on a
range of tiers rather than extrapolated.

The construction mirrors the adapter-level template in
`tests/unit/execution/test_orphan_fk.py`: a hand-built `SeedEnvelope` plan plus a
`RelationshipGraph`, fed straight to `PandasExecutionAdapter.run`. That isolates
masking plus full-frame materialization (the cost the design doc analyses, and the
surface Option 2 changes) from profiling and I/O.

Reused by `scripts/fk_memory_probe.py` (measurement) and the Option 2 byte-parity
regression test.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import numpy as np
import pyarrow as pa

from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import (
    OrphanPolicy,
    RelationshipEdge,
    RelationshipGraph,
)
from decoy_engine.relationships._namespace import NamespaceRegistry

# Namespaces for the two FK edges in a 3-table chain. The parent key column and
# the child FK column that references it must share a namespace so the keyed mask
# reproduces the same masked value on both sides (this is the RI mechanism, see
# docs/relationships.md). A grandchild needs its own namespace for edge 2.
_NS_PARENT = "fk_ns_parent"
_NS_CHILD = "fk_ns_child"

_SEED = (0xABCD).to_bytes(8, "big")

# Default payload width per table beyond the key columns. A real wide schema (the
# healthcare example in the design doc) carries many non-key columns; full-frame
# holds all of them resident, so they are the dominant memory cost and must be
# present for the measurement to be faithful.
_DEFAULT_WIDTH = 16

# Cardinality of the random string pool that backs filler columns. High enough to
# create many distinct Python str objects (real object-column memory), small
# enough to generate fast at millions of rows.
_FILLER_POOL = 4096


def _hash_col(namespace: str) -> ColumnSeed:
    """A deterministic, value-keyed `hash` seed (CHUNK_SAFE), per the orphan-fk
    test template. Value-keyed masking is what preserves the FK join."""
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


def _string_pool(rng: np.random.Generator, n: int, width: int = 12) -> np.ndarray:
    """A pool of `n` fixed-width ASCII strings for filler columns."""
    alphabet = np.frombuffer(b"abcdefghijklmnopqrstuvwxyz0123456789", dtype="S1")
    idx = rng.integers(0, len(alphabet), size=(n, width))
    return np.array([b"".join(row).decode("ascii") for row in alphabet[idx]], dtype=object)


def _filler_columns(rng: np.random.Generator, rows: int, width: int) -> dict[str, Any]:
    """`width` object columns of moderate-cardinality strings, picked from a pool.

    Vectorized pick keeps generation fast at millions of rows while still
    materializing real Python str objects (so the full-frame memory is realistic).
    """
    pool = _string_pool(rng, _FILLER_POOL)
    cols: dict[str, Any] = {}
    for i in range(width):
        take = rng.integers(0, _FILLER_POOL, size=rows)
        cols[f"payload_{i:02d}"] = pool[take]
    return cols


def _keys(prefix: str, n: int) -> np.ndarray:
    """`n` distinct string primary keys, e.g. p0..p{n-1}. Vectorized."""
    return (prefix + np.arange(n).astype("U")).astype(object)


# Per-table rng offset so each table generates independently (and lazily, one at
# a time) while staying FK-consistent: keys are positional (p0..p{n-1}, c0..c{n-1}),
# so a child's FK column can reference its parent's keys without the parent resident.
_TABLE_SEED_OFFSET = {"parent": 0, "child": 1, "grandchild": 2}
_TABLE_NAMES = ("parent", "child", "grandchild")


def build_table(
    name: str,
    rows: int,
    *,
    width: int = _DEFAULT_WIDTH,
    orphan_frac: float = 0.0,
    seed: int = 20260630,
) -> pa.Table:
    """Build one table of the parent/child/grandchild chain independently.

    Keys are positional so FK columns reference the parent's keys without the
    parent table resident; `orphan_frac` of FK rows are planted with keys that
    have no parent. Used by both the eager `build_fk_relational` (all three at
    once, for byte-parity tests) and `lazy_loader` (one at a time, for the memory
    probe, so peak memory reflects single-table residency)."""
    if not 0.0 <= orphan_frac < 1.0:
        raise ValueError("orphan_frac must be in [0, 1)")
    if name not in _TABLE_SEED_OFFSET:
        raise ValueError(f"unknown table {name!r}")
    rng = np.random.default_rng(seed + _TABLE_SEED_OFFSET[name])
    n_orphan = int(rows * orphan_frac)

    def _fk(parent_prefix: str, orphan_prefix: str) -> np.ndarray:
        parent_keys = _keys(parent_prefix, rows)
        col = parent_keys[rng.integers(0, rows, size=rows)].copy()
        if n_orphan:
            col[rng.choice(rows, size=n_orphan, replace=False)] = _keys(orphan_prefix, n_orphan)
        return col

    if name == "parent":
        return pa.table({"id": _keys("p", rows), **_filler_columns(rng, rows, width)})
    if name == "child":
        return pa.table(
            {
                "id": _keys("c", rows),
                "parent_id": _fk("p", "orphan_p"),
                **_filler_columns(rng, rows, width),
            }
        )
    return pa.table({"child_id": _fk("c", "orphan_c"), **_filler_columns(rng, rows, width)})


def make_plan() -> Any:
    """The (data-independent) seed plan for the 3-table chain."""
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("parent", TableSeed(per_column=(("id", _hash_col(_NS_PARENT)),), per_group=())),
                (
                    "child",
                    TableSeed(
                        per_column=(
                            ("id", _hash_col(_NS_CHILD)),
                            ("parent_id", _hash_col(_NS_PARENT)),
                        ),
                        per_group=(),
                    ),
                ),
                (
                    "grandchild",
                    TableSeed(per_column=(("child_id", _hash_col(_NS_CHILD)),), per_group=()),
                ),
            ),
        )
    )


def make_graph(orphan_policy: OrphanPolicy) -> RelationshipGraph:
    """The parent->child->grandchild relationship graph for an orphan policy."""
    return RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("id",),
                child_table="child",
                child_columns=("parent_id",),
                namespace=_NS_PARENT,
                orphan_policy=orphan_policy,
            ),
            RelationshipEdge(
                parent_table="child",
                parent_columns=("id",),
                child_table="grandchild",
                child_columns=("child_id",),
                namespace=_NS_CHILD,
                orphan_policy=orphan_policy,
            ),
        ),
        ordering=(),
    )


def lazy_loader(
    rows: int,
    *,
    width: int = _DEFAULT_WIDTH,
    orphan_frac: float = 0.0,
    seed: int = 20260630,
) -> Any:
    """A `source_loader(table)` for `run_sequential` that builds each table on
    demand, so the three tables are never all resident at once. This is what lets
    the memory probe measure the sequential path's real single-table ceiling."""

    def load(table: str) -> pa.Table:
        return build_table(table, rows, width=width, orphan_frac=orphan_frac, seed=seed)

    return load


@dataclass(frozen=True)
class FkFixture:
    """A generated multi-table FK chain ready for `PandasExecutionAdapter.run`.

    `sources` maps table name to its Arrow table; `plan` and `namespace_registry`
    and `registry` are the other `run` arguments. `graph(policy)` builds the
    relationship graph for a chosen orphan policy. `orphan_rows` records how many
    orphan FK rows were planted (per FK edge) for assertion in measurement.
    """

    sources: dict[str, pa.Table]
    plan: Any
    namespace_registry: NamespaceRegistry
    registry: Any
    rows: int
    orphan_rows: int

    def graph(self, orphan_policy: OrphanPolicy) -> RelationshipGraph:
        return make_graph(orphan_policy)


def build_fk_relational(
    rows: int,
    *,
    width: int = _DEFAULT_WIDTH,
    orphan_frac: float = 0.0,
    seed: int = 20260630,
) -> FkFixture:
    """Build a parent to child to grandchild FK chain of `rows` rows per table.

    Each table carries `width` wide payload columns plus its key columns. FK
    columns reference the parent's primary key by sampling it; `orphan_frac` of
    child and grandchild FK rows are planted with keys that have no parent (to
    exercise orphan policy on the full-frame path).
    """
    if not 0.0 <= orphan_frac < 1.0:
        raise ValueError("orphan_frac must be in [0, 1)")
    n_orphan = int(rows * orphan_frac)
    sources = {
        name: build_table(name, rows, width=width, orphan_frac=orphan_frac, seed=seed)
        for name in _TABLE_NAMES
    }
    return FkFixture(
        sources=sources,
        plan=make_plan(),
        namespace_registry=NamespaceRegistry(bindings=()),
        registry=get_default_registry(),
        rows=rows,
        orphan_rows=n_orphan,
    )
