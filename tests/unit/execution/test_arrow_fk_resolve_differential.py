"""Differential tests: the Arrow-native FK resolve in `_mixed_fk_bridge` must match
the original per-row Python resolve (`_resolve_python`, which drives the shared
`resolve_fk_keys` engine the oracle uses) on values, exact Arrow dtype,
warnings and error code/message."""

from __future__ import annotations

import pyarrow as pa
import pytest

import decoy_engine.execution._mixed_fk_bridge as bridge
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge
from tests.parity._f2_mixed_support import ipc_bytes

POLICIES = list(OrphanPolicy)
U63 = 2**63 + 1


def _edge(policy: OrphanPolicy) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="p",
        parent_columns=("id",),
        child_table="c",
        child_columns=("pid",),
        namespace="ns",
        orphan_policy=policy,
    )


def _run(fn, *args):
    try:
        return ("ok", fn(*args))
    except ExecutionError as exc:
        return ("err", (exc.code, str(exc)))


def _assert_same(parent: pa.Array, child: pa.Array, policy: OrphanPolicy, *, arrow: bool) -> None:
    pt = pa.table({"id": parent})
    ct = pa.table({"pid": child})
    edge = _edge(policy)
    gated = bridge._arrow_common_key_type(child.type, parent.type) is not None
    assert gated is arrow
    got = _run(
        lambda: bridge.resolve_generate_parent_fk_child(edge, parent_table=pt, child_table=ct)
    )
    want = _run(bridge._resolve_python, edge, pt, ct)
    assert got[0] == want[0]
    if got[0] == "err":
        assert got[1] == want[1]
        return
    g, w = got[1], want[1]
    assert g.column.type == w.column.type
    assert g.column.to_pylist() == w.column.to_pylist()
    assert g.column.equals(w.column)
    # Canonical IPC bytes (schema + values) so a string vs large_string or
    # null-type divergence fails even when the values compare equal.
    assert ipc_bytes(pa.table({"pid": g.column})) == ipc_bytes(pa.table({"pid": w.column}))
    assert g.warnings == w.warnings


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize(
    ("parent", "child", "arrow"),
    [
        # signed vs unsigned mix delegates
        (pa.array([1, U63], pa.uint64()), pa.array([-1, 1], pa.int32()), False),
        (pa.array([1, 5], pa.int64()), pa.array([U63, 1], pa.uint64()), False),
        # cross-family delegates
        (pa.array(["1", "2"]), pa.array([1, 2, 3], pa.int64()), False),
        (pa.array([1, 2], pa.int64()), pa.array(["1", "2", "9"]), False),
        # same-signedness width mixes take the Arrow path
        (pa.array([1, 2, 3], pa.int64()), pa.array([1, 3, 9, None], pa.int32()), True),
        (pa.array([1, 2, 3], pa.int32()), pa.array([1, 3, 9, None], pa.int64()), True),
        (pa.array([1, 2, U63], pa.uint64()), pa.array([2, 4_000_000_000, 7], pa.uint32()), True),
        (pa.array([1, 2], pa.uint32()), pa.array([2, 2**40], pa.uint64()), True),
        # uint64 boundary stays exact
        (pa.array([U63, 3], pa.uint64()), pa.array([U63, U63, 3], pa.uint64()), True),
        (pa.array([U63, 3], pa.uint64()), pa.array([U63 + 2, 3], pa.uint64()), True),
        # empty child
        (pa.array([1, 2], pa.int64()), pa.array([], pa.int64()), True),
        (pa.array([1, 2], pa.int32()), pa.array([], pa.int32()), True),
        (pa.array(["a"]), pa.array([], pa.string()), True),
        # all-null child
        (pa.array([1, 2], pa.int64()), pa.array([None, None], pa.int64()), True),
        (pa.array([1, 2], pa.int64()), pa.array([None, None], pa.int32()), True),
        (pa.array(["a"]), pa.array([None, None], pa.string()), True),
        (pa.array(["a"]), pa.array([None], pa.large_string()), True),
        # strings, mixed widths, orphans
        (pa.array(["a", "b"]), pa.array(["a", "z", None, "b"], pa.large_string()), True),
        (pa.array(["a", "b"], pa.large_string()), pa.array(["a", "z"], pa.string()), True),
        # duplicate parent keys
        (pa.array([1, 1, 2, 2], pa.int64()), pa.array([2, 1, 7], pa.int64()), True),
        (pa.array(["x", "x"]), pa.array(["x", "y"]), True),
        # null child + null parent: no spurious match, null child is not an orphan
        (pa.array([1, None, 2], pa.int64()), pa.array([None, 1, 5], pa.int64()), True),
        (pa.array(["a", None]), pa.array([None, "a", "q"]), True),
        # matched only (REMAP/FAIL must succeed)
        (pa.array([1, 2, 3], pa.int64()), pa.array([3, None, 1], pa.int64()), True),
        # int32 + null
        (pa.array([1, 2], pa.int32()), pa.array([1, None], pa.int32()), True),
    ],
)
def test_arrow_matches_python_resolve(
    parent: pa.Array, child: pa.Array, arrow: bool, policy
) -> None:
    _assert_same(parent, child, policy, arrow=arrow)


@pytest.mark.parametrize(
    ("ptype", "ctype"),
    [
        (pa.float64(), pa.float64()),
        (pa.bool_(), pa.bool_()),
        (pa.decimal128(10, 0), pa.decimal128(10, 0)),
        (pa.dictionary(pa.int32(), pa.string()), pa.string()),
    ],
)
def test_other_types_delegate(ptype: pa.DataType, ctype: pa.DataType) -> None:
    assert bridge._arrow_common_key_type(ctype, ptype) is None


def test_empty_child_orphan_count_is_zero_not_none() -> None:
    for policy in POLICIES:
        out = bridge.resolve_generate_parent_fk_child(
            _edge(policy),
            parent_table=pa.table({"id": pa.array([1], pa.int64())}),
            child_table=pa.table({"pid": pa.array([], pa.int64())}),
        )
        assert len(out.column) == 0
        assert out.warnings == ()
