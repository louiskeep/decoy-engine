"""F32 T-E2 and T-E4: labels come from position, agree across sections, and never share state."""

from __future__ import annotations

import threading

import pytest

from decoy_engine.reasons import route
from decoy_engine.support import (
    ErrorFact,
    LabelAllocator,
    Outcome,
    RetryClass,
    RouteFact,
    RouteKind,
    TableShape,
)

MARKERS = [f"MARKER_T_{i:04x}" for i in range(1, 6)]


def _walk(alloc: LabelAllocator, tables: list[str], nodes: list[str]) -> list[str]:
    out = [alloc.node(n) for n in nodes]
    for t in tables:
        out.append(alloc.table(t))
        out.extend(alloc.column(t, f"{t}_col_{j}") for j in range(3))
    return out


def test_same_table_same_label_across_sections() -> None:
    alloc = LabelAllocator()
    node = alloc.node("MARKER_N_beef")
    fact = RouteFact(
        node=node,
        table=alloc.table(MARKERS[0]),
        route=RouteKind.UNKNOWN,
        native_admitted=None,
        declines=(),
    )
    err = ErrorFact(
        node=alloc.node("MARKER_N_beef"),
        table=alloc.table(MARKERS[0]),
        reason=route.FK_RELATIONSHIP.spec.ref,
        outcome=Outcome.EXECUTION,
        retry_class=RetryClass.NEVER,
    )
    shape = TableShape(label=alloc.table(MARKERS[0]), rows_log2=None, columns=())
    assert fact.table == err.table == shape.label == "t1"
    assert fact.node == err.node == node == "n1"
    assert alloc.table(MARKERS[1]) == "t2"
    assert alloc.column(MARKERS[0], "MARKER_C_1") == "t1.c1"
    assert alloc.column(MARKERS[1], "MARKER_C_1") == "t2.c1"
    assert alloc.column(MARKERS[0], "MARKER_C_2") == "t1.c2"
    assert alloc.column(MARKERS[0], "MARKER_C_1") == "t1.c1"


def test_label_from_position_not_bytes() -> None:
    other = [f"renamed_{i}" * 3 for i in range(len(MARKERS))]
    a = _walk(LabelAllocator(), MARKERS, ["MARKER_N_1", "MARKER_N_2"])
    b = _walk(LabelAllocator(), other, ["x", "y"])
    assert a == b
    assert a[:4] == ["n1", "n2", "t1", "t1.c1"]
    assert not any("MARKER" in label for label in a)


def test_concurrent_builds_independent() -> None:
    n = 2000
    barrier = threading.Barrier(2)
    results: dict[int, list[str]] = {}

    def build(k: int) -> None:
        alloc = LabelAllocator()
        barrier.wait()
        results[k] = [alloc.table(f"MARKER_T_{k}_{i}") for i in range(n)]

    threads = [threading.Thread(target=build, args=(k,)) for k in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    expected = [f"t{i}" for i in range(1, n + 1)]
    assert results == {0: expected, 1: expected}


def test_ordinal_cap() -> None:
    many = LabelAllocator()
    for i in range(9999):
        many.table(i)
    with pytest.raises(ValueError):
        many.table("one more")
