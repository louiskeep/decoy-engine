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

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution.out_of_core._source import LazySource
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


# BENCH-CODE-1 (roadmap OOC-C sibling; decoy-platform NEXT-RUNS-HANDOFF.md R4):
# the masking strategies `scripts/fk_memory_probe.py`'s `--strategy` knob can
# apply to `payload_NN` filler columns, so one run measures per-strategy cost
# on an otherwise-identical FK-linked frame. FK key columns always use
# `_hash_col` above regardless of this choice -- RI must hold no matter which
# strategy the payload columns exercise.
PAYLOAD_STRATEGIES = ("hash", "fpe", "faker", "categorical")

# hash is the only strategy this fixture module exercised before this knob
# existed (it is what `_hash_col` already applies to every FK key column), so
# it is the default: the closest reading of "preserve today's behavior" the
# fixed 4-choice knob allows. Masking payload columns AT ALL by default is new
# (they previously passed through unmasked -- `make_plan`'s `width=0` default
# below reproduces that exactly), but extending the one strategy the probe
# already exercised, rather than introducing a second one as the default, is
# the smallest behavior change; see `fk_memory_probe.py --help` for the same
# note at the CLI surface.
DEFAULT_PAYLOAD_STRATEGY = "hash"

# Bounded category pool for the `categorical` strategy. Arbitrary labels: the
# strategy remaps ANY source value onto this pool via
# `derive_index(job_seed, namespace, value, pool_size=len(categories))`
# (`CategoricalStrategyHandler`), so the pool's content is irrelevant to the
# cost being measured, only its size.
_PAYLOAD_CATEGORIES = [f"cat_{i:02d}" for i in range(32)]


def _payload_col(strategy: str, namespace: str) -> ColumnSeed:
    """A `payload_NN` filler-column `ColumnSeed` for one of `PAYLOAD_STRATEGIES`.

    Each config mirrors a working example already proven in this repo's own
    strategy tests (fpe: tests/unit/execution/test_fpe_strategy.py; faker:
    tests/unit/execution/test_faker_strategy.py; categorical:
    tests/unit/execution/test_categorical_weighted.py), so the probe measures
    the real engine transform cost, not a probe-local reimplementation.
    """
    if strategy == "hash":
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
    if strategy == "fpe":
        # "alphanum" == "0123456789abcdefghijklmnopqrstuvwxyz" (fpe.py's
        # _CHARSETS), the exact alphabet `_string_pool` draws from below, so
        # every payload value round-trips through the Feistel permutation
        # instead of hitting the out-of-charset fail-closed path.
        return ColumnSeed(
            namespace=namespace,
            strategy="fpe",
            provider="fpe",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=True,
            provider_config=(("charset", "alphanum"),),
            coherent_with=(),
        )
    if strategy == "faker":
        return ColumnSeed(
            namespace=namespace,
            strategy="faker",
            provider="person_email",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=True,
            provider_config=(("pool_size", 256),),
            coherent_with=(),
        )
    if strategy == "categorical":
        return ColumnSeed(
            namespace=namespace,
            strategy="categorical",
            provider=None,
            backend_type="decoy_native",
            backend_version="1",
            cardinality_mode="bijective",
            deterministic=True,
            provider_config=(("categories", _PAYLOAD_CATEGORIES),),
            coherent_with=(),
        )
    raise ValueError(f"unknown payload strategy {strategy!r}; choose one of {PAYLOAD_STRATEGIES}")


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


def make_plan(width: int = 0, strategy: str = DEFAULT_PAYLOAD_STRATEGY) -> Any:
    """The seed plan for the 3-table chain.

    FK key columns are always hash-masked (RI-preserving, independent of
    `strategy`). `width` additionally seeds that many `payload_NN` columns per
    table with `strategy` (BENCH-CODE-1's `--strategy` knob). `width=0` (the
    default, and what every caller that predates BENCH-CODE-1 still passes
    implicitly) omits payload seeding entirely -- the per_table tuples below
    are then byte-identical to this function's shape before BENCH-CODE-1.
    """
    payload = {
        table: tuple(
            (f"payload_{i:02d}", _payload_col(strategy, f"fk_ns_payload_{table}"))
            for i in range(width)
        )
        for table in _TABLE_NAMES
    }
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "parent",
                    TableSeed(
                        per_column=(("id", _hash_col(_NS_PARENT)), *payload["parent"]),
                        per_group=(),
                    ),
                ),
                (
                    "child",
                    TableSeed(
                        per_column=(
                            ("id", _hash_col(_NS_CHILD)),
                            ("parent_id", _hash_col(_NS_PARENT)),
                            *payload["child"],
                        ),
                        per_group=(),
                    ),
                ),
                (
                    "grandchild",
                    TableSeed(
                        per_column=(("child_id", _hash_col(_NS_CHILD)), *payload["grandchild"]),
                        per_group=(),
                    ),
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


# Row-chunk size for `write_large_fk_chain`'s own writer loop, independent of the
# out-of-core runner's batch sizing. Small enough that a modest `rows` count still
# spans several row groups (so "never whole-table-resident" is exercised by tests
# rather than incidental), large enough not to dominate write time at scale.
_CHUNK_ROWS = 50_000


def write_table_parquet(
    name: str,
    rows: int,
    path: Any,
    *,
    width: int = _DEFAULT_WIDTH,
    orphan_frac: float = 0.0,
    seed: int = 20260630,
) -> Path:
    """Write `build_table`'s output straight to Parquet, byte-equal on read-back.

    Backs parity tests that need an on-disk (`LazySource`) input which is
    otherwise identical to the in-memory fixture the byte-parity suite already
    exercises.
    """
    out_path = Path(path)
    table = build_table(name, rows, width=width, orphan_frac=orphan_frac, seed=seed)
    pq.write_table(table, out_path)
    return out_path


def _chunked_schema(name: str, width: int) -> pa.Schema:
    payload_fields = [pa.field(f"payload_{i:02d}", pa.string()) for i in range(width)]
    if name == "parent":
        return pa.schema([pa.field("id", pa.string()), *payload_fields])
    if name == "child":
        return pa.schema(
            [pa.field("id", pa.string()), pa.field("parent_id", pa.string()), *payload_fields]
        )
    return pa.schema([pa.field("child_id", pa.string()), *payload_fields])


def _keys_from_idx(prefix: str, idx: np.ndarray) -> np.ndarray:
    """Vectorized `{prefix}{i}` keys for an arbitrary (not necessarily 0-based)
    row-index array, the same positional scheme `_keys` uses for a full range."""
    return (prefix + idx.astype("U")).astype(object)


def _fk_from_idx(
    prefix: str,
    idx: np.ndarray,
    rows: int,
    orphan_every: int,
    orphan_prefix: str,
) -> np.ndarray:
    """FK values computed from the child/grandchild row index alone.

    `idx % rows` always lands on an existing parent index (parents are indexed
    0..rows-1), so referential integrity holds by construction without the
    parent's key array resident to sample from, and without any RNG state that
    must replay identically across chunks. Every `orphan_every`-th row is
    planted with a key that has no parent, giving an exact, chunk-boundary-
    independent orphan fraction.
    """
    keys = _keys_from_idx(prefix, idx % rows)
    if orphan_every:
        is_orphan = (idx % orphan_every) == 0
        if is_orphan.any():
            keys = keys.copy()
            keys[is_orphan] = _keys_from_idx(orphan_prefix, idx[is_orphan])
    return keys


def _chunk_table(
    name: str,
    start: int,
    length: int,
    rows: int,
    width: int,
    orphan_every: int,
    rng: np.random.Generator,
    pool: np.ndarray,
) -> pa.Table:
    idx = np.arange(start, start + length)
    cols: dict[str, Any] = {}
    if name == "parent":
        cols["id"] = _keys_from_idx("p", idx)
    elif name == "child":
        cols["id"] = _keys_from_idx("c", idx)
        cols["parent_id"] = _fk_from_idx("p", idx, rows, orphan_every, "orphan_p")
    else:
        cols["child_id"] = _fk_from_idx("c", idx, rows, orphan_every, "orphan_c")
    for i in range(width):
        take = rng.integers(0, len(pool), size=length)
        cols[f"payload_{i:02d}"] = pool[take]
    return pa.table(cols, schema=_chunked_schema(name, width))


def _write_table_chunked(
    name: str,
    rows: int,
    path: Path,
    *,
    width: int,
    orphan_every: int,
    batch_rows: int,
    seed: int,
) -> None:
    """Generate and write one table's rows in `batch_rows`-sized chunks.

    Each chunk is built, written, and dropped before the next is built, so
    Python/Arrow never holds more than one chunk of this table at a time
    (unlike `build_table`, which returns the whole table at once).
    """
    rng = np.random.default_rng(seed + _TABLE_SEED_OFFSET[name])
    pool = _string_pool(rng, _FILLER_POOL)
    schema = _chunked_schema(name, width)
    writer = pq.ParquetWriter(path, schema)
    try:
        for start in range(0, rows, batch_rows):
            length = min(batch_rows, rows - start)
            writer.write_table(
                _chunk_table(name, start, length, rows, width, orphan_every, rng, pool)
            )
    finally:
        writer.close()


def write_large_fk_chain(
    dir_path: Any,
    rows: int,
    *,
    width: int = _DEFAULT_WIDTH,
    orphan_frac: float = 0.0,
    batch_rows: int = _CHUNK_ROWS,
    seed: int = 20260630,
) -> dict[str, Path]:
    """Write a parent->child->grandchild FK chain straight to Parquet, one
    row-chunk at a time, so no whole table is ever resident.

    This is what lets a capability-proof test build a dataset larger than a
    memory cap: unlike `build_fk_relational` (which returns whole in-memory
    tables) this generator's peak residency is one `batch_rows` chunk per
    table, regardless of `rows`. Column schema (id/parent_id/child_id plus
    payload_NN filler) matches `build_table`, so the same plan/graph in this
    module applies unchanged. Byte-parity to `build_table` is not a goal here:
    keys are a row-index formula rather than a sampled draw (see
    `_fk_from_idx`), because FK integrity at a scale where the pandas oracle
    would OOM is what this generator is for, not byte-for-byte match against
    the eager fixture.
    """
    if not 0.0 <= orphan_frac < 1.0:
        raise ValueError("orphan_frac must be in [0, 1)")
    out_dir = Path(dir_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    orphan_every = round(1 / orphan_frac) if orphan_frac > 0 else 0

    paths = {name: out_dir / f"{name}.parquet" for name in _TABLE_NAMES}
    for name in _TABLE_NAMES:
        _write_table_chunked(
            name,
            rows,
            paths[name],
            width=width,
            orphan_every=orphan_every,
            batch_rows=batch_rows,
            seed=seed,
        )
    return paths


def lazy_sources(paths: Mapping[str, Any]) -> dict[str, LazySource]:
    """Wrap written table paths as `LazySource`s for the out-of-core runner."""
    return {name: LazySource(path=Path(path)) for name, path in paths.items()}
