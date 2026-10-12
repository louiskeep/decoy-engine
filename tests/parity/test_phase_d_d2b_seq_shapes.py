"""Phase D / D2b: SEQ hash routing across the acyclic FK shapes, proven with a stand-in kernel.

No companion is needed. `MarkingKernel` returns the Python token with a prefix, so a column
counts as native only when the emitted table carries the marked value. Each shape is compared
with the full-frame oracle (a different route that never reaches the SEQ kernel): the SEQ output
must equal the oracle's tokens with the mark added to every kernel-derived one, and the stand-in
must be called for exactly the rows the shape's parents and orphans own, never for a matched
child. The real-kernel parity and the strict dimensions live in `test_phase_d_d2b_seq_real.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter, _sequential, _sequential_native
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import build_work_list
from decoy_engine.execution._sequential_native import SEQ_HASH_DISPOSITION_KEY, SeqNativeRun
from decoy_engine.execution._sequential_preflight import (
    COMPOSITE_CHAIN_CODE,
    check_composite_fk_chain,
)
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity._d2a_seq_data import RecordingSink, table_digest
from tests.parity._seq_witness import MARK, MarkingKernel, install, spy_python_hash

REMAP, PRESERVE = OrphanPolicy.REMAP, OrphanPolicy.PRESERVE

# (id, shape builder, rows of every kernel call, sorted). Parents hash once; each orphan adds
# one REMAP row; a matched child adds nothing.
_CASES: list[tuple[str, Callable[[], s.Shape], list[int]]] = [
    ("chain-remap", lambda: s.chain_shape(REMAP), [1, 1, 1]),
    ("chain-preserve", lambda: s.chain_shape(PRESERVE), [1]),
    ("diamond-preserve", lambda: s.diamond_shape(PRESERVE), [2, 3, 3]),
    ("diamond-remap", lambda: s.diamond_shape(REMAP), [1, 2, 3, 3]),
    ("multi-parent-preserve", lambda: s.multi_parent_shape(PRESERVE), [2, 2]),
    ("multi-parent-remap", lambda: s.multi_parent_shape(REMAP), [1, 2, 2]),
    ("self-ref-remap", lambda: s.self_ref_shape(REMAP), [1, 4]),
    ("self-ref-preserve", lambda: s.self_ref_shape(PRESERVE), [4]),
    ("junction-preserve", lambda: s.junction_shape(PRESERVE), [2, 2]),
    ("junction-remap", lambda: s.junction_shape(REMAP), [1, 1, 2, 2]),
    ("composite-remap", lambda: s.composite_shape(REMAP), [1, 1, 3, 3]),
    ("composite-preserve", lambda: s.composite_shape(PRESERVE), [3, 3]),
]
_IDS = [c[0] for c in _CASES]


def _expected(shape: s.Shape, unmarked: dict[str, frozenset[str]] | None = None) -> dict[str, Any]:
    oracle = s.run_full(shape).outputs
    unmarked = unmarked or {}
    return {
        t: {
            c: s.marked_like(oracle[t], c, MARK, unmarked=unmarked.get(t, frozenset()))
            for c in oracle[t].column_names
        }
        for t in oracle
    }


def _emitted(result: Any) -> dict[str, Any]:
    return {t: out.to_pydict() for t, out in result.outputs.items()}


@pytest.mark.parametrize(("name", "build", "rows"), _CASES, ids=_IDS)
def test_each_shape_is_consumed_from_the_kernel_and_matches_the_oracle(
    name: str, build: Callable[[], s.Shape], rows: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = build()
    expected = _expected(shape)
    kernel = install(monkeypatch, MarkingKernel())
    python_rows = spy_python_hash(monkeypatch)
    result = s.run_seq(shape)
    assert _emitted(result) == expected, name
    assert sorted(c.rows for c in kernel.calls) == rows, name
    assert python_rows == [], name
    for out_table, oracle_table in zip(
        result.outputs.values(), s.run_full(shape).outputs.values(), strict=True
    ):
        assert [f.type for f in out_table.schema] == [f.type for f in oracle_table.schema]


def _paths(result: Any) -> set[tuple[str, str, str, str]]:
    return {
        (e["table"], e["column"], e["path"], e["executed_backend"])
        for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY]
    }


@pytest.mark.parametrize(
    ("build", "native", "inherited"),
    [
        (
            lambda: s.chain_shape(REMAP),
            {("A", "k"), ("B", "k"), ("C", "k")},
            {("B", "k"), ("C", "k")},
        ),
        (
            lambda: s.diamond_shape(PRESERVE),
            {("R", "k"), ("L", "lid"), ("M", "mid")},
            {("L", "k"), ("M", "k"), ("D", "lk"), ("D", "mk")},
        ),
        (
            lambda: s.multi_parent_shape(PRESERVE),
            {("P1", "k"), ("P2", "k")},
            {("X", "ref")},
        ),
        (lambda: s.self_ref_shape(PRESERVE), {("E", "id")}, {("E", "mgr")}),
        (
            lambda: s.junction_shape(PRESERVE),
            {("A", "id"), ("B", "id")},
            {("J", "a"), ("J", "b")},
        ),
        (
            lambda: s.composite_shape(PRESERVE),
            {("P", "k"), ("P", "j")},
            {("C", "k"), ("C", "j")},
        ),
    ],
    ids=["chain", "diamond", "multi-parent", "self-ref", "junction", "composite"],
)
def test_witness_names_the_native_parents_and_the_inherited_children(
    build: Callable[[], s.Shape],
    native: set[tuple[str, str]],
    inherited: set[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel())
    paths = _paths(s.run_seq(build()))
    scalar = {(t, c) for t, c, p, b in paths if p == "scalar" and b == "rust_companion"}
    assert scalar == {k for k in native if k not in inherited}
    assert {(t, c) for t, c, p, b in paths if p == "fk_inherited" and b == "inherited"} == inherited


# Chain: the middle column is both a child and a parent key.


def test_chain_remaps_a_genuine_orphan_at_every_hop_on_the_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    python_rows = spy_python_hash(monkeypatch)
    result = s.run_seq(s.chain_shape(REMAP))
    out = result.outputs
    # Orphan markers per table: A has none, B's orphan is its second row, C's its third.
    assert [v.startswith(MARK) for v in out["B"].column("k").to_pylist()] == [True, True]
    assert [v.startswith(MARK) for v in out["C"].column("k").to_pylist()] == [True, True, True]
    # One kernel call per hop, one row each: A hashes `a`, B mints `b`, C mints `c`. A matched
    # child that were rehashed would show up as a call with more rows.
    assert [c.rows for c in kernel.calls] == [1, 1, 1]
    assert python_rows == []
    remaps = {
        (e["table"], e["column"]): e["executed_backend"]
        for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY]
        if e["path"] == "fk_remap"
    }
    assert remaps == {("A", "k"): "rust_companion", ("B", "k"): "rust_companion"}


def test_chain_hops_with_several_matched_children_never_rehash_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = s.chain_shape(
        REMAP,
        a=["a1", "a2", "a3"],
        b=["a1", "a2", "a3", "b"],
        c=["a1", "a2", "a3", "b", "c1", "c2"],
    )
    expected = _expected(shape)
    kernel = install(monkeypatch, MarkingKernel())
    result = s.run_seq(shape)
    assert _emitted(result) == expected
    assert [c.rows for c in kernel.calls] == [3, 1, 2]


def test_a_discarded_middle_column_remap_decision_is_caught_by_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The D2a model (one decision per non-child column) leaves C's orphan to Python."""
    kernel = install(monkeypatch, MarkingKernel())
    real = SeqNativeRun.prepare_table

    def execution_decisions_only(self: SeqNativeRun, *args: Any) -> None:
        real(self, *args)
        self._remap_decisions = {
            k: v for k, v in self._remap_decisions.items() if k in self._decisions
        }

    monkeypatch.setattr(SeqNativeRun, "prepare_table", execution_decisions_only)
    python_rows = spy_python_hash(monkeypatch)
    out = s.run_seq(s.chain_shape(REMAP)).outputs
    assert [v.startswith(MARK) for v in out["C"].column("k").to_pylist()] == [True, True, False]
    assert python_rows == [1]
    assert [c.rows for c in kernel.calls] == [1, 1]


def test_a_declined_hop_stays_python_while_the_native_hop_before_it_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = s.mixed_type_chain_shape(REMAP)
    oracle = s.run_full(shape).outputs
    kernel = install(monkeypatch, MarkingKernel())
    python_rows = spy_python_hash(monkeypatch)
    result = s.run_seq(shape)
    got = _emitted(result)
    # A and B are native end to end, B's orphan included. C's orphan (4) is minted by B.k, a
    # decimal column the kernel declines, so it is the plain Python token.
    assert got["A"]["k"] == s.marked_like(oracle["A"], "k", MARK)
    assert got["B"]["k"] == s.marked_like(oracle["B"], "k", MARK)
    plain = oracle["C"].column("k").to_pylist()
    assert got["C"]["k"] == [MARK + v for v in plain[:3]] + [plain[3]]
    assert [c.rows for c in kernel.calls] == [2, 1]
    assert python_rows == [1]
    remaps = {
        (e["table"], e["column"]): e
        for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY]
        if e["path"] == "fk_remap"
    }
    assert remaps[("A", "k")]["executed_backend"] == "rust_companion"
    assert remaps[("B", "k")]["executed_backend"] == "python_scalar"
    assert remaps[("B", "k")]["reason"] == "hash_remap_parent_stays_python:k"


# Multi-parent: first-hit-wins, with per-parent tokens that cannot be confused.


def test_multi_parent_child_inherits_the_first_declared_parents_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    out = s.run_seq(s.multi_parent_shape(PRESERVE)).outputs
    p1, p2 = out["P1"].column("k").to_pylist(), out["P2"].column("k").to_pylist()
    assert p1[1] != p2[1]  # the shared key hashes differently under each parent's namespace
    ref = out["X"].column("ref").to_pylist()
    assert ref == [p1[0], p2[0], p1[1], "nowhere"]
    assert ref[2] != p2[1]
    assert [c.rows for c in kernel.calls] == [2, 2]


def test_reversing_the_declared_parent_order_changes_the_shared_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the precedence assertion above would fail if first-hit-wins were reversed."""
    install(monkeypatch, MarkingKernel())
    shape = s.multi_parent_shape(PRESERVE)
    reversed_graph = s.graph(*reversed(shape.graph.edges))
    out = s.run_seq(s.Shape(shape.plan, reversed_graph, shape.sources)).outputs
    assert out["X"].column("ref").to_pylist()[2] == out["P2"].column("k").to_pylist()[1]


def test_multi_parent_policy_conflict_still_fails_at_graph_build() -> None:
    from datetime import datetime

    from decoy_engine.plan._errors import PlanCompileError
    from decoy_engine.profile._types import Profile, Relationship
    from decoy_engine.relationships import build_namespace_registry, build_relationship_graph

    rels = tuple(
        Relationship(
            parent_table=p,
            parent_columns=("k",),
            child_table="X",
            child_columns=("ref",),
            namespace="ns_x",
        )
        for p in ("P1", "P2")
    )
    profile = Profile(
        schema_version=1,
        tables=(),
        relationships=rels,
        profiled_at=datetime(2026, 6, 12),
        decoy_engine_version="0.1.0",
    )
    lookup: dict[tuple[str, tuple[str, ...], str, tuple[str, ...]], OrphanPolicy] = {
        ("P1", ("k",), "X", ("ref",)): OrphanPolicy.REMAP,
        ("P2", ("k",), "X", ("ref",)): OrphanPolicy.FAIL,
    }
    with pytest.raises(PlanCompileError) as exc:
        build_relationship_graph(
            rels,
            namespace_registry=build_namespace_registry({"tables": []}, profile),
            orphan_policy_lookup=lookup,
        )
    assert exc.value.code == "orphan_policy_conflict"


# Self-reference: the per-node drain keeps working next to a native hash column.


def test_self_ref_hash_is_native_beside_a_row_erroring_column(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from tests.parity._d1a_hash_data import column_seed

    date_shift = column_seed(
        "date_shift", namespace="ns_d", provider_config=(("min_days", 1), ("max_days", 30))
    )
    plan = s.make_plan(
        {"E": s.table_seed({"id": s.hashed("ns_emp"), "mgr": s.hashed("ns_emp"), "d": date_shift})}
    )
    shape = s.self_ref_shape(REMAP)
    src = {
        "E": pa.table(
            {
                "id": s.strs(["e1", "e2", "e3", "e4"]),
                "mgr": s.strs([None, "e1", "e2", "ghost"]),
                "d": s.strs(["2020-01-01", "notadate", "2020-01-03", "2020-01-04"]),
            }
        )
    }
    quarantine = {
        "enabled": True,
        "output_path": str(tmp_path / "q.jsonl"),
        "triggers": ["format_error"],
    }
    kernel = install(monkeypatch, MarkingKernel())
    result = PandasExecutionAdapter().run_sequential(
        plan,
        lambda t: src[t],
        registry=s.REGISTRY,
        relationship_graph=shape.graph,
        namespace_registry=s.NAMESPACES,
        quarantine_config=quarantine,
    )
    out = result.outputs["E"]
    assert len(result.row_errors) == 1
    assert out.num_rows == 3  # the errored row is quarantined, never emitted
    assert all(v is None or v.startswith(MARK) for v in out.column("id").to_pylist())
    assert sorted(c.rows for c in kernel.calls) == [1, 4]


# Single-level composite.


def test_composite_child_inherits_the_native_tuple_and_remaps_each_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    out = s.run_seq(s.composite_shape(REMAP)).outputs
    p = out["P"].to_pydict()
    c = out["C"].to_pydict()
    assert list(zip(c["k"][:2], c["j"][:2], strict=True)) == list(
        zip(p["k"][:2], p["j"][:2], strict=True)
    )
    # The orphan tuple is minted per member on the kernel; the partial-null tuple stays null.
    assert c["k"][2].startswith(MARK) and c["j"][2].startswith(MARK)
    assert c["k"][3] is None and c["j"][3] is None
    assert sorted(x.rows for x in kernel.calls) == [1, 1, 3, 3]


def test_composite_tuple_with_one_declined_member_is_a_native_python_mix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = s.mixed_composite_shape(REMAP)
    expected = _expected(shape, {"P": frozenset({"j"}), "C": frozenset({"j"})})
    kernel = install(monkeypatch, MarkingKernel())
    python_rows = spy_python_hash(monkeypatch)
    result = s.run_seq(shape)
    assert _emitted(result) == expected
    assert sorted(c.rows for c in kernel.calls) == [1, 3]  # k parent, k orphan
    assert python_rows == [3, 1]  # j parent, j orphan
    by_member = {
        (e["table"], e["column"], e["path"]): e["executed_backend"]
        for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY]
    }
    assert by_member[("P", "k", "scalar")] == "rust_companion"
    assert by_member[("P", "j", "scalar")] == "python_scalar"
    assert by_member[("P", "k", "fk_remap")] == "rust_companion"
    assert by_member[("P", "j", "fk_remap")] == "python_scalar"


def test_composite_root_with_a_nullable_integer_member_is_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel())
    shape = s.composite_shape(PRESERVE)
    sources = dict(shape.sources)
    sources["P"] = pa.table({"k": s.strs(["a", "b", "c"]), "j": pa.array([1, None, 3], pa.int64())})
    sources["C"] = pa.table({"k": s.strs(["a"]), "j": pa.array([1], pa.int64())})
    with pytest.raises(ExecutionError) as exc:
        s.run_seq(s.Shape(shape.plan, shape.graph, sources))
    assert exc.value.code == "null_bearing_int_unsupported"


# Composite multi-hop chain: declined up front.


def test_composite_chain_with_remap_is_declined_before_any_table_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    asked: list[str] = []
    shape = s.composite_chain_shape(REMAP)
    sink = RecordingSink()

    def loader(name: str) -> pa.Table:
        asked.append(name)
        return shape.sources[name]

    with pytest.raises(ExecutionError) as exc:
        PandasExecutionAdapter().run_sequential(
            shape.plan,
            loader,
            registry=s.REGISTRY,
            relationship_graph=shape.graph,
            namespace_registry=s.NAMESPACES,
            sink=sink,  # type: ignore[arg-type]
        )
    assert exc.value.code == COMPOSITE_CHAIN_CODE == "sequential_composite_fk_chain_unsupported"
    assert asked == []
    assert kernel.calls == []
    assert (sink.staged, sink.committed, sink.commits) == ({}, {}, 0)


def test_composite_chain_decline_names_identifiers_and_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel())
    shape = s.composite_chain_shape(REMAP)
    sources = {
        t: pa.table({"k": s.strs(["CELLVALUE1", "CELLVALUE2"]), "j": s.strs(["x", "y"])})
        for t in "ABC"
    }
    with pytest.raises(ExecutionError) as exc:
        s.run_seq(s.Shape(shape.plan, shape.graph, sources))
    text = str(exc.value) + exc.value.message
    assert "'C'" in text and "'B'" in text and "'k'" in text and "'j'" in text
    assert "CELLVALUE" not in text and "orphan_remap_parent_missing" not in text
    assert exc.value.__cause__ is None  # a fixed code, not a wrapped internal exception


@pytest.mark.parametrize(
    "shape_builder",
    [
        lambda: s.composite_chain_shape(REMAP),
        lambda: s.composite_chain_shape(
            REMAP, b_members=("j", "k"), c_members=("j", "k"), b_edge_parent=("j", "k")
        ),
        lambda: s.composite_chain_shape(REMAP, c_members=("j", "k"), b_edge_parent=("k", "j")),
    ],
    ids=["same-order", "reordered-groups", "reordered-child"],
)
def test_composite_chain_predicate_is_member_based(shape_builder: Callable[[], s.Shape]) -> None:
    shape = shape_builder()
    with pytest.raises(ExecutionError) as exc:
        check_composite_fk_chain(build_work_list(shape.plan, s.REGISTRY), shape.graph)
    assert exc.value.code == COMPOSITE_CHAIN_CODE


def _overlapping_chain(parent_cols: tuple[str, ...], child_cols: tuple[str, ...]) -> s.Shape:
    """B carries a composite group (k, j) from A plus its own scalar `m`."""
    plan = s.make_plan(
        {
            "A": s.table_seed({"k": s.hashed("ns_ak"), "j": s.hashed("ns_aj")}),
            "B": s.table_seed({"m": s.hashed("ns_bm")}, {("k", "j"): "ns_bg"}),
            "C": s.table_seed(
                {c: s.hashed("ns_c") for c in child_cols if len(child_cols) == 1},
                {child_cols: "ns_cg"} if len(child_cols) > 1 else None,
            ),
        }
    )
    g = s.graph(
        s.edge("A", ("k", "j"), "B", ("k", "j"), "ns_bg", REMAP),
        s.edge("B", parent_cols, "C", child_cols, "ns_cg", REMAP),
    )
    return s.Shape(plan, g, {})


def test_partially_overlapping_group_is_declined_when_a_member_is_a_parent_key() -> None:
    shape = _overlapping_chain(("k", "m"), ("k", "m"))
    with pytest.raises(ExecutionError) as exc:
        check_composite_fk_chain(build_work_list(shape.plan, s.REGISTRY), shape.graph)
    assert exc.value.code == COMPOSITE_CHAIN_CODE
    assert "'k'" in exc.value.message


def test_a_scalar_parent_key_on_a_table_with_a_group_is_not_declined() -> None:
    shape = _overlapping_chain(("m",), ("m",))
    check_composite_fk_chain(build_work_list(shape.plan, s.REGISTRY), shape.graph)


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.WARN])
def test_composite_chain_without_remap_is_not_declined_and_matches_the_oracle(
    policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = s.composite_chain_shape(policy)
    src = dict(shape.sources)
    src["C"] = pa.table({"k": s.strs(["a", "b", "zz"]), "j": s.strs(["x", "y", "zz"])})
    shape = s.Shape(shape.plan, shape.graph, src)
    expected = _expected(shape)
    install(monkeypatch, MarkingKernel())
    assert _emitted(s.run_seq(shape)) == expected


def test_the_oracle_still_raises_the_raw_code_for_the_declined_shape() -> None:
    shape = s.composite_chain_shape(REMAP)
    src = dict(shape.sources)
    src["C"] = pa.table({"k": s.strs(["a", "zz"]), "j": s.strs(["x", "zz"])})
    with pytest.raises(ExecutionError) as exc:
        s.run_full(s.Shape(shape.plan, shape.graph, src))
    assert exc.value.code == "orphan_remap_parent_missing"


# Unset / non-hash byte identity, guards and abort.


def test_a_chain_with_no_hash_column_loads_no_kernel_and_adds_no_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    shape = s.chain_shape(REMAP)
    plan = s.make_plan({t: s.table_seed({"k": s.passthrough()}) for t in ("A", "B", "C")})
    result = s.run_seq(s.Shape(plan, shape.graph, shape.sources))
    oracle = s.run_full(s.Shape(plan, shape.graph, shape.sources)).outputs
    assert kernel.calls == []
    assert SEQ_HASH_DISPOSITION_KEY not in result.quality_metrics
    for name, table in result.outputs.items():
        assert table_digest(table) == table_digest(oracle[name])


def test_kernel_fault_at_a_late_hop_aborts_the_sink_with_nothing_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel(fail_on_call=3))
    python_rows = spy_python_hash(monkeypatch)
    sink = RecordingSink()
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        s.run_seq(s.chain_shape(REMAP), sink=sink)
    assert (sink.commits, sink.aborts, sink.committed) == (0, 1, {})
    assert python_rows == []  # the failed hop is not retried in Python


def test_a_successful_chain_commits_once_through_a_transactional_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel())
    sink = RecordingSink()
    s.run_seq(s.chain_shape(REMAP), sink=sink)
    assert (sink.commits, sink.aborts, sorted(sink.committed)) == (1, 0, ["A", "B", "C"])


def test_reason_codes_and_the_error_carry_no_cell_values(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, MarkingKernel())
    result = s.run_seq(s.mixed_type_chain_shape(REMAP))
    text = repr(result.quality_metrics)
    for needle in ("Decimal", "'4'"):
        assert needle not in text


# Module size.


def test_new_logic_stays_out_of_the_two_capped_modules() -> None:
    def loc(module: Any) -> int:
        with open(module.__file__, encoding="utf-8") as fh:
            return sum(1 for _ in fh)

    from decoy_engine.execution import _pandas_adapter, _sequential_preflight

    assert loc(_sequential_native) <= 600
    assert loc(_sequential_preflight) <= 600
    assert loc(_sequential) <= 700
    assert loc(_pandas_adapter) <= 700
