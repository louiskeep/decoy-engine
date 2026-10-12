"""Phase D / D2b: shared builders for the SEQ route's acyclic FK shapes.

Every shape is a small hand-built plan plus relationship graph, run through the SEQ route
(`run_sequential`) and the full-frame oracle (`run`) with the same inputs. Key values are
plain strings so the only thing that varies between runs is who computed the token.

A composite child carries its member columns in a `GroupSeed` on `per_group` and not in
`per_column`, exactly as the planner compiles it (`plan/_seed_envelope.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, GroupSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from tests.parity._d1a_hash_data import _PASSTHROUGH, JOB_SEED, column_seed

REGISTRY = get_default_registry()
NAMESPACES = NamespaceRegistry(bindings=())


def hashed(namespace: str) -> ColumnSeed:
    return column_seed(namespace=namespace)


def passthrough() -> ColumnSeed:
    return _PASSTHROUGH


def table_seed(
    columns: dict[str, ColumnSeed], groups: dict[tuple[str, ...], str] | None = None
) -> TableSeed:
    """`groups` maps a composite child's member columns to the group namespace."""
    per_group = tuple(
        ("__".join(sorted(cols)), GroupSeed(namespace=ns, coherent_columns=cols))
        for cols, ns in (groups or {}).items()
    )
    return TableSeed(per_column=tuple(columns.items()), per_group=per_group)


def make_plan(tables: dict[str, TableSeed]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(job_seed=JOB_SEED, per_table=tuple(tables.items()))
    )


def edge(
    parent: str,
    parent_cols: str | tuple[str, ...],
    child: str,
    child_cols: str | tuple[str, ...],
    namespace: str,
    policy: OrphanPolicy = OrphanPolicy.PRESERVE,
) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=parent,
        parent_columns=(parent_cols,) if isinstance(parent_cols, str) else parent_cols,
        child_table=child,
        child_columns=(child_cols,) if isinstance(child_cols, str) else child_cols,
        namespace=namespace,
        orphan_policy=policy,
    )


def graph(*edges: RelationshipEdge) -> RelationshipGraph:
    return RelationshipGraph(edges=tuple(edges), ordering=())


@dataclass(frozen=True)
class Shape:
    """A plan, its graph and its source tables: everything a route needs."""

    plan: Any
    graph: RelationshipGraph
    sources: dict[str, pa.Table]


def run_seq(
    shape: Shape, *, adapter: PandasExecutionAdapter | None = None, sink: Any = None
) -> Any:
    srcs = shape.sources
    return (adapter or PandasExecutionAdapter()).run_sequential(
        shape.plan,
        lambda t: srcs[t],
        registry=REGISTRY,
        relationship_graph=shape.graph,
        namespace_registry=NAMESPACES,
        sink=sink,
    )


def run_full(shape: Shape) -> Any:
    return PandasExecutionAdapter().run(
        shape.plan,
        shape.sources,
        registry=REGISTRY,
        relationship_graph=shape.graph,
        namespace_registry=NAMESPACES,
    )


def strs(values: Sequence[str | None]) -> pa.Array:
    return pa.array(values, pa.string())


# Scalar shapes.


def chain_shape(
    policy: OrphanPolicy = OrphanPolicy.REMAP,
    *,
    a: list[str] | None = None,
    b: list[str] | None = None,
    c: list[str] | None = None,
) -> Shape:
    """A.k -> B.k -> C.k with one genuine orphan at each hop by default.

    B holds a key A does not (`b`), C holds a key B does not (`c`); B.k is both an FK child
    and a parent key, which is the case D2a's decision model could not serve natively."""
    a = a or ["a"]
    b = b or ["a", "b"]
    c = c or ["a", "b", "c"]
    plan = make_plan(
        {
            "A": table_seed({"k": hashed("ns_ab")}),
            "B": table_seed({"k": hashed("ns_bc")}),
            "C": table_seed({"k": hashed("ns_bc")}),
        }
    )
    g = graph(edge("A", "k", "B", "k", "ns_ab", policy), edge("B", "k", "C", "k", "ns_bc", policy))
    return Shape(
        plan,
        g,
        {
            "A": pa.table({"k": strs(a)}),
            "B": pa.table({"k": strs(b)}),
            "C": pa.table({"k": strs(c)}),
        },
    )


def diamond_shape(policy: OrphanPolicy = OrphanPolicy.PRESERVE) -> Shape:
    """R -> L, R -> M, L -> D, M -> D. L holds one key (r9) that R does not."""
    plan = make_plan(
        {
            "R": table_seed({"k": hashed("ns_r")}),
            "L": table_seed({"k": hashed("ns_l"), "lid": hashed("ns_lid")}),
            "M": table_seed({"k": hashed("ns_m"), "mid": hashed("ns_mid")}),
            "D": table_seed({"lk": hashed("ns_dl"), "mk": hashed("ns_dm")}),
        }
    )
    g = graph(
        edge("R", "k", "L", "k", "ns_r", policy),
        edge("R", "k", "M", "k", "ns_r", policy),
        edge("L", "lid", "D", "lk", "ns_dl", policy),
        edge("M", "mid", "D", "mk", "ns_dm", policy),
    )
    return Shape(
        plan,
        g,
        {
            "R": pa.table({"k": strs(["r1", "r2", "r3"])}),
            "L": pa.table({"k": strs(["r1", "r2", "r9"]), "lid": strs(["l1", "l2", "l3"])}),
            "M": pa.table({"k": strs(["r2", "r3"]), "mid": strs(["m1", "m2"])}),
            "D": pa.table({"lk": strs(["l1", "l2", "l1"]), "mk": strs(["m1", "m2", "m2"])}),
        },
    )


def multi_parent_shape(policy: OrphanPolicy = OrphanPolicy.PRESERVE) -> Shape:
    """Child column X.ref declares two parents (P1.k, P2.k) with OVERLAPPING source keys.

    "shared" exists in both parents under different namespaces, so the two parents'
    tokens for it differ and first-hit-wins (declared edge order) is observable."""
    plan = make_plan(
        {
            "P1": table_seed({"k": hashed("ns_p1")}),
            "P2": table_seed({"k": hashed("ns_p2")}),
            "X": table_seed({"ref": hashed("ns_x")}),
        }
    )
    g = graph(
        edge("P1", "k", "X", "ref", "ns_x", policy),
        edge("P2", "k", "X", "ref", "ns_x", policy),
    )
    return Shape(
        plan,
        g,
        {
            "P1": pa.table({"k": strs(["only1", "shared"])}),
            "P2": pa.table({"k": strs(["only2", "shared"])}),
            "X": pa.table({"ref": strs(["only1", "only2", "shared", "nowhere"])}),
        },
    )


def self_ref_shape(policy: OrphanPolicy = OrphanPolicy.REMAP) -> Shape:
    """E.mgr -> E.id on one table, with a null and an orphan manager."""
    plan = make_plan(
        {"E": table_seed({"id": hashed("ns_emp"), "mgr": hashed("ns_emp"), "n": passthrough()})}
    )
    g = graph(edge("E", "id", "E", "mgr", "ns_emp", policy))
    return Shape(
        plan,
        g,
        {
            "E": pa.table(
                {
                    "id": strs(["e1", "e2", "e3", "e4"]),
                    "mgr": strs([None, "e1", "e2", "ghost"]),
                    "n": strs(["w", "x", "y", "z"]),
                }
            )
        },
    )


def junction_shape(policy: OrphanPolicy = OrphanPolicy.PRESERVE) -> Shape:
    """J.a -> A.id and J.b -> B.id: one table with two distinct FKs."""
    plan = make_plan(
        {
            "A": table_seed({"id": hashed("ns_a")}),
            "B": table_seed({"id": hashed("ns_b")}),
            "J": table_seed({"a": hashed("ns_a"), "b": hashed("ns_b")}),
        }
    )
    g = graph(edge("A", "id", "J", "a", "ns_a", policy), edge("B", "id", "J", "b", "ns_b", policy))
    return Shape(
        plan,
        g,
        {
            "A": pa.table({"id": strs(["a1", "a2"])}),
            "B": pa.table({"id": strs(["b1", "b2"])}),
            "J": pa.table(
                {"a": strs(["a1", "a2", "a9", None]), "b": strs(["b1", "b8", "b2", "b1"])}
            ),
        },
    )


# Composite shapes.


def composite_shape(
    policy: OrphanPolicy = OrphanPolicy.REMAP, *, parent_seed_j: ColumnSeed | None = None
) -> Shape:
    """P.(k, j) -> C.(k, j): a ROOT composite parent and a composite child."""
    plan = make_plan(
        {
            "P": table_seed({"k": hashed("ns_pk"), "j": parent_seed_j or hashed("ns_pj")}),
            "C": table_seed({}, {("k", "j"): "ns_cg"}),
        }
    )
    g = graph(edge("P", ("k", "j"), "C", ("k", "j"), "ns_cg", policy))
    return Shape(
        plan,
        g,
        {
            "P": pa.table({"k": strs(["a", "b", "c"]), "j": strs(["x", "y", "z"])}),
            "C": pa.table({"k": strs(["a", "b", "q", None]), "j": strs(["x", "y", "w", "x"])}),
        },
    )


def composite_chain_shape(
    policy: OrphanPolicy = OrphanPolicy.REMAP,
    *,
    b_members: tuple[str, ...] = ("k", "j"),
    c_members: tuple[str, ...] = ("k", "j"),
    b_edge_parent: tuple[str, ...] = ("k", "j"),
) -> Shape:
    """A.(k, j) -> B.(k, j) -> C.(k, j): a composite child that is also a composite parent.

    `b_members` / `c_members` set the member order of each group, and `b_edge_parent` the
    order in which the second edge names B's columns, so reordered groups can be built."""
    plan = make_plan(
        {
            "A": table_seed({"k": hashed("ns_ak"), "j": hashed("ns_aj")}),
            "B": table_seed({}, {b_members: "ns_bg"}),
            "C": table_seed({}, {c_members: "ns_cg"}),
        }
    )
    g = graph(
        edge("A", ("k", "j"), "B", b_members, "ns_bg", policy),
        edge("B", b_edge_parent, "C", c_members, "ns_cg", policy),
    )
    cols = {"k": strs(["a", "b"]), "j": strs(["x", "y"])}
    return Shape(
        plan,
        g,
        {"A": pa.table(cols), "B": pa.table(cols), "C": pa.table(cols)},
    )


def mixed_type_chain_shape(policy: OrphanPolicy = OrphanPolicy.REMAP) -> Shape:
    """A.k (int64, admitted) -> B.k (decimal, declined) -> C.k (decimal).

    The first hop's parent is native-eligible and the second hop's is not, so REMAP has to
    pick a backend per hop."""
    import decimal

    dec = pa.decimal128(5, 0)

    def d(*v: int) -> pa.Array:
        return pa.array([decimal.Decimal(x) for x in v], dec)

    plan = make_plan(
        {
            "A": table_seed({"k": hashed("ns_ab")}),
            "B": table_seed({"k": hashed("ns_bc")}),
            "C": table_seed({"k": hashed("ns_bc")}),
        }
    )
    g = graph(edge("A", "k", "B", "k", "ns_ab", policy), edge("B", "k", "C", "k", "ns_bc", policy))
    return Shape(
        plan,
        g,
        {
            "A": pa.table({"k": pa.array([1, 2], pa.int64())}),
            "B": pa.table({"k": d(1, 2, 3)}),
            "C": pa.table({"k": d(1, 2, 3, 4)}),
        },
    )


def mixed_composite_shape(policy: OrphanPolicy = OrphanPolicy.REMAP) -> Shape:
    """P.(k, j) -> C.(k, j) where k is a string (admitted) and j a decimal (declined)."""
    import decimal

    dec = pa.decimal128(5, 0)

    def d(*v: int | None) -> pa.Array:
        return pa.array([None if x is None else decimal.Decimal(x) for x in v], dec)

    plan = make_plan(
        {
            "P": table_seed({"k": hashed("ns_pk"), "j": hashed("ns_pj")}),
            "C": table_seed({}, {("k", "j"): "ns_cg"}),
        }
    )
    g = graph(edge("P", ("k", "j"), "C", ("k", "j"), "ns_cg", policy))
    return Shape(
        plan,
        g,
        {
            "P": pa.table({"k": strs(["a", "b", "c"]), "j": d(1, 2, 3)}),
            "C": pa.table({"k": strs(["a", "b", "q"]), "j": d(1, 2, 9)}),
        },
    )


def is_token(value: object) -> bool:
    """A derived hash token: 64 lowercase hex characters."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value)
    )


def marked_like(
    table: pa.Table, name: str, mark: str, *, unmarked: frozenset[str] = frozenset()
) -> list[object]:
    """`table`'s column with `mark` prefixed to every derived token, as a stand-in kernel returns.

    `unmarked` names columns that must stay Python tokens (a declined type)."""
    values = table.column(name).to_pylist()
    if name in unmarked:
        return values
    return [mark + v if is_token(v) else v for v in values]


def golden_sources(fixture: str, **string_columns: tuple[str, ...]) -> dict[str, pa.Table]:
    """The CSV tables of `tests/fixtures/golden/<fixture>`; named columns are read as strings.

    `string_columns` maps a table name to the columns kept as text, so a date-looking key does
    not change type between the parent and the child file."""
    from pathlib import Path

    import pyarrow.csv as pacsv

    root = Path(__file__).parents[1] / "fixtures" / "golden" / fixture
    out: dict[str, pa.Table] = {}
    for path in sorted(root.glob("*.csv")):
        types = {c: pa.string() for c in string_columns.get(path.stem, ())}
        out[path.stem] = pacsv.read_csv(
            path, convert_options=pacsv.ConvertOptions(column_types=types)
        )
    return out
