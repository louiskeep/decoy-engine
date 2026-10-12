"""Phase D / D2a: shared fixtures for the sequential (SEQ) route hash contract.

The chain is the D1a chain (`up.pk -> t.fk`, `t.hk -> child.ck`) so the payload tokens can
reuse the D1a literals, which were captured from the pure-Python `hash_array` and never from
the output under test. SEQ differs from the out-of-core route in one input rule: it rejects an
integer column that holds a null (`null_bearing_int_unsupported`), so the typed payload here
fills the one null per integer column with its neighbour's value and the expectation copies the
neighbour's literal token. String and bool columns keep their nulls.

Helpers run the same plan through `PandasExecutionAdapter.run_sequential` (SEQ) and
`PandasExecutionAdapter.run` (full-frame), with a plain callable sink, a recording
transactional sink, or no sink.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import (
    _PASSTHROUGH,
    EXPECTED_TOKENS,
    JOB_SEED,
    chain_graph,
    chain_plan,
    chain_sources,
    column_seed,
    hash_seeds,
    source_columns,
)

REGISTRY = get_default_registry()
NAMESPACES = NamespaceRegistry(bindings=())


def typed_columns() -> tuple[dict[str, pa.Array], dict[str, list[str | None]]]:
    """The D1a typed columns with every integer null filled, and the matching token literals."""
    cols = source_columns()
    expected = {name: list(tokens) for name, tokens in EXPECTED_TOKENS.items()}
    for name, arr in list(cols.items()):
        if not pa.types.is_integer(arr.type):
            continue
        values = arr.to_pylist()
        hole = values.index(None)
        values[hole] = values[hole + 1]
        cols[name] = pa.array(values, arr.type)
        expected[name][hole] = expected[name][hole + 1]
    return cols, expected


def payload_table() -> pa.Table:
    cols, _ = typed_columns()
    return pa.table({**cols, "c_trunc": cols["c_utf8"], "c_other_ns": cols["c_utf8"]})


def payload_seeds() -> tuple[tuple[str, ColumnSeed], ...]:
    cols, _ = typed_columns()
    return (
        *hash_seeds(list(cols)),
        ("c_trunc", column_seed(provider_config=(("truncate", 12),))),
        ("c_other_ns", column_seed(namespace="ns_other")),
    )


def with_when(expression: str, strategy: ColumnSeed | None = None) -> ColumnSeed:
    return replace(strategy or column_seed(), when=expression)


def sources_with_orphans(payload: pa.Table, orphans: list[str]) -> dict[str, pa.Table]:
    """The chain sources with extra child keys that no parent row owns."""
    srcs = chain_sources(payload)
    keys = srcs["child"].column("ck").to_pylist()
    srcs["child"] = pa.table({"ck": pa.array([*keys, *orphans], pa.string())})
    return srcs


def table_digest(table: pa.Table) -> str:
    """A version-stable whole-table digest: schema signature plus every strict cell token."""
    h = hashlib.sha256(sc.schema_signature(table).encode())
    for name in table.column_names:
        h.update(name.encode())
        h.update(repr([sc._cell_token(v) for v in table.column(name).to_pylist()]).encode())
    return h.hexdigest()


@dataclass
class RecordingSink:
    """A transactional sink (write/commit/abort) that records what each call saw.

    `fail_on_write` names a table whose write raises, to model a fault after earlier staged
    writes. Staged tables only become `committed` on `commit()`."""

    fail_on_write: str | None = None
    staged: dict[str, pa.Table] = field(default_factory=dict)
    committed: dict[str, pa.Table] = field(default_factory=dict)
    commits: int = 0
    aborts: int = 0

    def write(self, table: str, data: pa.Table) -> None:
        if table == self.fail_on_write:
            raise RuntimeError("injected sink fault")
        self.staged[table] = data

    def commit(self) -> None:
        self.commits += 1
        self.committed = dict(self.staged)

    def abort(self) -> None:
        self.aborts += 1
        self.staged = {}


def run_seq(
    payload: pa.Table,
    seeds: tuple[tuple[str, ColumnSeed], ...],
    *,
    policy: OrphanPolicy = OrphanPolicy.PRESERVE,
    sources: dict[str, pa.Table] | None = None,
    sink: Any = None,
    adapter: PandasExecutionAdapter | None = None,
) -> Any:
    srcs = sources if sources is not None else chain_sources(payload)
    return (adapter or PandasExecutionAdapter()).run_sequential(
        chain_plan(seeds),
        lambda t: srcs[t],
        registry=REGISTRY,
        relationship_graph=chain_graph(policy),
        namespace_registry=NAMESPACES,
        sink=sink,
    )


def run_full(
    payload: pa.Table,
    seeds: tuple[tuple[str, ColumnSeed], ...],
    *,
    policy: OrphanPolicy = OrphanPolicy.PRESERVE,
    sources: dict[str, pa.Table] | None = None,
) -> Any:
    srcs = sources if sources is not None else chain_sources(payload)
    return PandasExecutionAdapter().run(
        chain_plan(seeds),
        srcs,
        registry=REGISTRY,
        relationship_graph=chain_graph(policy),
        namespace_registry=NAMESPACES,
    )


def run_plain() -> Any:
    """A one-table SEQ run with no hash column: passthrough and truncate only."""
    from types import SimpleNamespace

    src = pa.table({"a": pa.array(["x", "y", None]), "b": pa.array(["abcdef", "gh", None])})
    truncate = column_seed("truncate", namespace=None, provider_config=(("length", 3),))
    table_seed = TableSeed(per_column=(("a", _PASSTHROUGH), ("b", truncate)), per_group=())
    plan: Any = SimpleNamespace(
        seed_envelope=SeedEnvelope(job_seed=JOB_SEED, per_table=(("t", table_seed),))
    )
    return PandasExecutionAdapter().run_sequential(
        plan,
        lambda _name: src,
        registry=REGISTRY,
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NAMESPACES,
    )


def callable_sink(into: dict[str, pa.Table]) -> Callable[[str, pa.Table], None]:
    def sink(table: str, out: pa.Table) -> None:
        into[table] = out

    return sink
