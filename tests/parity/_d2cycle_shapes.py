"""Phase D / D2-cycle: shape builders for cross-table FK cycles on the bounded SEQ route.

Every shape is a hand-built plan + relationship graph + source tables, run through the SEQ
route (`run_sequential` with a `CycleContext`) and the full-frame oracle (`run`). The column
graph of each shape is acyclic while its TABLE graph cycles, which is the only cycle the
full-frame path masks today.

Naming: `A.id -> B.mid -> A.back` is the rev-1 counterexample chain. `id` is a producer (no
incoming edge), `mid` is a child that is also a parent, `back` is a child.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pyarrow as pa

from decoy_engine.execution._sequential import run_sequential
from decoy_engine.execution._sequential_cycle_admit import CycleContext
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d2b_shapes import (
    NAMESPACES,
    REGISTRY,
    Shape,
    edge,
    graph,
    hashed,
    make_plan,
    passthrough,
    run_full,
    strs,
    table_seed,
)

__all__ = [
    "BIG_BUDGET",
    "Shape",
    "chain_cycle_shape",
    "cycle_ctx",
    "mixed_topology_shape",
    "run_full",
    "run_seq_cycle",
    "three_table_cycle_shape",
    "two_table_cycle_shape",
]

BIG_BUDGET = 8 * 1024**3
PRESERVE = OrphanPolicy.PRESERVE


def cycle_ctx(
    shape: Shape,
    *,
    budget: int | None = BIG_BUDGET,
    transforms: frozenset[str] = frozenset(),
    projected: bool = True,
    calls: list[tuple[str, tuple[str, ...]]] | None = None,
    bytes_of: dict[str, int] | None = None,
) -> CycleContext:
    """A context sized from the shape's own sources. `calls` records every projected read."""
    srcs = shape.sources

    def projected_loader(table: str, columns: Sequence[str]) -> pa.Table:
        if calls is not None:
            calls.append((table, tuple(columns)))
        return srcs[table].select(list(columns))

    return CycleContext(
        budget_bytes=budget,
        table_rows={t: tbl.num_rows for t, tbl in srcs.items()},
        table_bytes=bytes_of or {t: tbl.nbytes for t, tbl in srcs.items()},
        transform_tables=transforms,
        projected_loader=projected_loader if projected else None,
    )


def run_seq_cycle(
    shape: Shape,
    *,
    ctx: CycleContext | None = None,
    sink: Any = None,
    quarantine: dict[str, Any] | None = None,
    loads: list[str] | None = None,
    pool_cache: Any = None,
) -> Any:
    """Run `shape` on the SEQ route. `loads` records every whole-table load."""
    from decoy_engine.execution import PandasExecutionAdapter

    srcs = shape.sources

    def loader(table: str) -> pa.Table:
        if loads is not None:
            loads.append(table)
        return srcs[table]

    return run_sequential(
        PandasExecutionAdapter(),
        shape.plan,
        loader,
        registry=REGISTRY,
        relationship_graph=shape.graph,
        namespace_registry=NAMESPACES,
        sink=sink,
        quarantine_config=quarantine,
        pool_cache=pool_cache,
        cycle_context=ctx if ctx is not None else cycle_ctx(shape),
    )


def two_table_cycle_shape(
    policy: OrphanPolicy = PRESERVE,
    *,
    id_seed: ColumnSeed | None = None,
    back_seed: ColumnSeed | None = None,
    mid_seed: ColumnSeed | None = None,
    a_id: list[str | None] | None = None,
    b_mid: list[str | None] | None = None,
    a_back: list[str | None] | None = None,
    extra_a: dict[str, tuple[ColumnSeed, pa.Array]] | None = None,
) -> Shape:
    """A.id -> B.mid -> A.back, with a duplicate producer key, a null and genuine orphans.

    B.mid holds a key A.id lacks (`aX`), so the first hop has an orphan; A.back holds that same
    orphan key (so the second hop matches an orphan the first hop PRESERVEd raw, the rev-1
    counterexample) and one key nothing owns (`zz`)."""
    a_id = a_id if a_id is not None else ["a1", "a2", "a3", "a2"]
    b_mid = b_mid if b_mid is not None else ["a1", "a2", "aX", None]
    a_back = a_back if a_back is not None else ["a1", "aX", "zz", None]
    a_seeds = {
        "id": id_seed or hashed("ns_ab"),
        "back": back_seed or hashed("ns_ba"),
        "pay": hashed("ns_pay"),
    }
    a_cols = {
        "id": strs(a_id),
        "back": strs(a_back),
        "pay": strs([f"p{i}" for i in range(len(a_id))]),
    }
    for name, (seed, values) in (extra_a or {}).items():
        a_seeds[name] = seed
        a_cols[name] = values
    plan = make_plan(
        {
            "A": table_seed(a_seeds),
            "B": table_seed({"mid": mid_seed or hashed("ns_ba"), "pay2": passthrough()}),
        }
    )
    g = graph(
        edge("A", "id", "B", "mid", "ns_ab", policy),
        edge("B", "mid", "A", "back", "ns_ba", policy),
    )
    return Shape(
        plan,
        g,
        {
            "A": pa.table(a_cols),
            "B": pa.table({"mid": strs(b_mid), "pay2": strs([f"q{i}" for i in range(len(b_mid))])}),
        },
    )


def three_table_cycle_shape(policy: OrphanPolicy = PRESERVE) -> Shape:
    """A.id -> B.x, B.y -> C.p, C.q -> A.z: three independent producers around one cycle."""
    plan = make_plan(
        {
            "A": table_seed({"id": hashed("ns_a"), "z": hashed("ns_c")}),
            "B": table_seed({"x": hashed("ns_a"), "y": hashed("ns_b")}),
            "C": table_seed({"p": hashed("ns_b"), "q": hashed("ns_c")}),
        }
    )
    g = graph(
        edge("A", "id", "B", "x", "ns_a", policy),
        edge("B", "y", "C", "p", "ns_b", policy),
        edge("C", "q", "A", "z", "ns_c", policy),
    )
    return Shape(
        plan,
        g,
        {
            "A": pa.table({"id": strs(["a1", "a2", "a3"]), "z": strs(["c1", "c2", "c9"])}),
            "B": pa.table({"x": strs(["a1", "a2", "a8"]), "y": strs(["b1", "b2", "b3"])}),
            "C": pa.table({"p": strs(["b1", "b2", "b7"]), "q": strs(["c1", "c2", "c3"])}),
        },
    )


def chain_cycle_shape(policy: OrphanPolicy = PRESERVE) -> Shape:
    """A.id -> B.m -> C.m -> A.back: a middle column at every hop, one producer."""
    plan = make_plan(
        {
            "A": table_seed({"id": hashed("ns_a"), "back": hashed("ns_c")}),
            "B": table_seed({"m": hashed("ns_b")}),
            "C": table_seed({"m": hashed("ns_c")}),
        }
    )
    g = graph(
        edge("A", "id", "B", "m", "ns_a", policy),
        edge("B", "m", "C", "m", "ns_b", policy),
        edge("C", "m", "A", "back", "ns_c", policy),
    )
    return Shape(
        plan,
        g,
        {
            "A": pa.table({"id": strs(["a1", "a2"]), "back": strs(["a1", "a2"])}),
            "B": pa.table({"m": strs(["a1", "a2", "a3"])}),
            "C": pa.table({"m": strs(["a1", "a2", "a4"])}),
        },
    )


def mixed_topology_shape(
    policy: OrphanPolicy = PRESERVE, *, p_key_when: str | None = None
) -> Shape:
    """P -> (A <-> B) -> C plus a disconnected D.

    P.k -> A.id (incoming acyclic parent), A.id -> B.mid and B.mid -> A.back (the cycle),
    B.mid -> C.ref (outgoing child). `p_key_when` gates P's hash so unmatched rows stay raw
    (the rev-1 `when:` false counterexample), the cycle's own keys stay unconditional."""
    p_k = hashed("ns_p")
    if p_key_when is not None:
        from dataclasses import replace

        p_k = replace(p_k, when=p_key_when)
    plan = make_plan(
        {
            "P": table_seed({"k": p_k, "gate": passthrough()}),
            "A": table_seed({"id": hashed("ns_pa"), "back": hashed("ns_ba")}),
            "B": table_seed({"mid": hashed("ns_ab")}),
            "C": table_seed({"ref": hashed("ns_bc")}),
            "D": table_seed({"x": hashed("ns_d")}),
        }
    )
    g = graph(
        edge("P", "k", "A", "id", "ns_pa", policy),
        edge("A", "id", "B", "mid", "ns_ab", policy),
        edge("B", "mid", "A", "back", "ns_ba", policy),
        edge("B", "mid", "C", "ref", "ns_bc", policy),
    )
    return Shape(
        plan,
        g,
        {
            "P": pa.table({"k": strs(["p1", "p2", "p3"]), "gate": strs(["on", "off", "on"])}),
            "A": pa.table({"id": strs(["p1", "p2", "p9"]), "back": strs(["p1", "p2", "p9"])}),
            "B": pa.table({"mid": strs(["p1", "p2", "p9"])}),
            "C": pa.table({"ref": strs(["p1", "p2", "p9", "p4"])}),
            "D": pa.table({"x": strs(["d1", "d2"])}),
        },
    )


def keyed(strategy: str, **config: Any) -> ColumnSeed:
    """A producer seed for one of the allowlisted strategies."""
    return column_seed(
        strategy,
        namespace=None if strategy == "passthrough" else "ns_k",
        provider_config=tuple(config.items()),
    )
