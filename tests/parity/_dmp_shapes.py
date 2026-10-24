"""Phase D / Dmp: multi-parent FK shapes and the out-of-core runner for their acceptance cases.

A multi-parent child declares one column tuple against N parents. Every builder here gives
each parent its OWN hash namespace, so a key present in two parents has two different tokens
and the first-hit-wins precedence is observable in the output (a reversed precedence cannot
hide behind equal tokens). `run_ooc` drives a `Shape` through `run_fk_out_of_core` resident,
on the sink path, and on the sink path with the external-reorder driver forced, reading sink
output back from the published parquet; it mirrors the Dcomp-OOC runner so the two slices'
contracts read the same way.
"""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s

# The out-of-core runners, the strict reports and the exact metadata contract are the
# Dcomp-OOC ones (`_dcomp_ooc_shapes`), re-exported so the Dmp suites read `m.<name>`.
from tests.parity._dcomp_ooc_shapes import (
    ALL_TRUE,
    FK_DTYPE_CODE,
    METADATA_ONLY,
    MODES,
    OOC_META,
    ORACLE_META,
    ORPHAN_CODE,
    OocRun,
    assert_agrees,
    assert_lane,
    hash_evidence,
    raised,
    run_ooc,
    sorted_warnings,
    strict,
    strict_modulo_pandas,
    warning_keys,
    without_pandas_key,
)

__all__ = [
    "ALL_TRUE",
    "FK_DTYPE_CODE",
    "METADATA_ONLY",
    "MODES",
    "OOC_META",
    "ORACLE_META",
    "ORPHAN_CODE",
    "OocRun",
    "assert_agrees",
    "assert_lane",
    "composite_multi_parent_shape",
    "hash_evidence",
    "int_passthrough_shape",
    "raised",
    "reversed_shape",
    "run_ooc",
    "sorted_warnings",
    "strict",
    "strict_modulo_pandas",
    "three_parent_shape",
    "warning_keys",
    "wide_shape",
    "without_pandas_key",
]


# Shapes. Parent `i` masks under namespace `ns_p{i}`; the child under `ns_x`.


def three_parent_shape(policy: OrphanPolicy) -> s.Shape:
    """P1.k, P2.k, P3.k -> X.ref. Every rank wins at least once; `all3` is in every parent,
    `p23` in the last two, and `nowhere` in none."""
    plan = s.make_plan(
        {
            "P1": s.table_seed({"k": s.hashed("ns_p1")}),
            "P2": s.table_seed({"k": s.hashed("ns_p2")}),
            "P3": s.table_seed({"k": s.hashed("ns_p3")}),
            "X": s.table_seed({"ref": s.hashed("ns_x")}),
        }
    )
    g = s.graph(
        s.edge("P1", "k", "X", "ref", "ns_x", policy),
        s.edge("P2", "k", "X", "ref", "ns_x", policy),
        s.edge("P3", "k", "X", "ref", "ns_x", policy),
    )
    return s.Shape(
        plan,
        g,
        {
            "P1": pa.table({"k": s.strs(["only1", "all3"])}),
            "P2": pa.table({"k": s.strs(["only2", "all3", "p23"])}),
            "P3": pa.table({"k": s.strs(["only3", "all3", "p23"])}),
            "X": pa.table(
                {"ref": s.strs(["only1", "only2", "only3", "all3", "p23", None, "nowhere"])}
            ),
        },
    )


def reversed_shape(shape: s.Shape) -> s.Shape:
    """The same job with the edges declared in the opposite order."""
    return s.Shape(shape.plan, s.graph(*reversed(shape.graph.edges)), shape.sources)


def composite_multi_parent_shape(policy: OrphanPolicy) -> s.Shape:
    """P1.(k, j) and P2.(k, j) -> C.(k, j): `s` is a shared tuple, `z` an orphan, one row null."""
    plan = s.make_plan(
        {
            "P1": s.table_seed({"k": s.hashed("ns_p1k"), "j": s.hashed("ns_p1j")}),
            "P2": s.table_seed({"k": s.hashed("ns_p2k"), "j": s.hashed("ns_p2j")}),
            "C": s.table_seed({}, {("k", "j"): "ns_cg"}),
        }
    )
    g = s.graph(
        s.edge("P1", ("k", "j"), "C", ("k", "j"), "ns_cg", policy),
        s.edge("P2", ("k", "j"), "C", ("k", "j"), "ns_cg", policy),
    )
    return s.Shape(
        plan,
        g,
        {
            "P1": pa.table({"k": s.strs(["a", "s"]), "j": s.strs(["x", "t"])}),
            "P2": pa.table({"k": s.strs(["b", "s"]), "j": s.strs(["y", "t"])}),
            "C": pa.table(
                {"k": s.strs(["a", "b", "s", "z", None]), "j": s.strs(["x", "y", "t", "z", "x"])}
            ),
        },
    )


def int_passthrough_shape(policy: OrphanPolicy, *, p2_type: pa.DataType | None = None) -> s.Shape:
    """P1.k (int64 passthrough) and P2.k (`p2_type` passthrough) -> X.ref (int64 passthrough).

    With `p2_type` unset both parents mask to int64 (admitted); with `float64` the two masked
    key types differ, the shape the merge refuses."""
    p2_type = p2_type or pa.int64()
    plan = s.make_plan(
        {
            "P1": s.table_seed({"k": s.passthrough()}),
            "P2": s.table_seed({"k": s.passthrough()}),
            "X": s.table_seed({"ref": s.passthrough()}),
        }
    )
    g = s.graph(
        s.edge("P1", "k", "X", "ref", "ns_x", policy), s.edge("P2", "k", "X", "ref", "ns_x", policy)
    )
    return s.Shape(
        plan,
        g,
        {
            "P1": pa.table({"k": pa.array([1, 2], pa.int64())}),
            "P2": pa.table({"k": pa.array([2, 3], p2_type)}),
            "X": pa.table({"ref": pa.array([1, 2, 3, 9, None], pa.int64())}),
        },
    )


def wide_shape(policy: OrphanPolicy, *, rows: int) -> s.Shape:
    """P1 holds the even keys and P2 every key; the child cycles through all of them plus an
    orphan every seventh row, so overlapping keys fall on every batch boundary."""
    keys = [f"k{i}" for i in range(rows)]
    child = [f"orphan{i}" if i % 7 == 0 else keys[i % rows] for i in range(rows * 2)]
    plan = s.make_plan(
        {
            "P1": s.table_seed({"k": s.hashed("ns_p1")}),
            "P2": s.table_seed({"k": s.hashed("ns_p2")}),
            "X": s.table_seed({"ref": s.hashed("ns_x")}),
        }
    )
    g = s.graph(
        s.edge("P1", "k", "X", "ref", "ns_x", policy), s.edge("P2", "k", "X", "ref", "ns_x", policy)
    )
    return s.Shape(
        plan,
        g,
        {
            "P1": pa.table({"k": s.strs(keys[::2])}),
            "P2": pa.table({"k": s.strs(keys)}),
            "X": pa.table({"ref": s.strs(child)}),
        },
    )
