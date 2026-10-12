"""Phase D / D2-cycle: a cross-table FK cycle on the bounded SEQ route equals the full-frame oracle.

The SEQ route masks an admitted cycle in two passes. Pass 1 reads only the cyclic key columns
and builds each parent's effective source->masked map in column order. Pass 2 masks every table
as usual and resolves its FK children from those maps. These tests pin the observable contract:
byte parity with `run` on the strict dimensions, the rev-1 to rev-4 counterexamples from the
plan, the narrow-read and map-release schedule, and the coded fail-closed declines.

The stand-in kernel (`MarkingKernel`) needs no companion. The real-kernel run is in
`test_phase_d_d2cycle_seq_real.py`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import _sequential, _sequential_cycle
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._sequential_cycle_admit import (
    BUDGET_EXCEEDED,
    KEY_STRATEGY,
    KEY_WRITER,
    PROJECTED_READ,
    QUARANTINE,
)
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d2a_seq_data import RecordingSink
from tests.parity._d2cycle_shapes import (
    chain_cycle_shape,
    cycle_ctx,
    mixed_topology_shape,
    run_full,
    run_seq_cycle,
    three_table_cycle_shape,
    two_table_cycle_shape,
)
from tests.parity._seq_witness import MARK, MarkingKernel, install, spy_python_hash

PRESERVE, REMAP, WARN, FAIL = (
    OrphanPolicy.PRESERVE,
    OrphanPolicy.REMAP,
    OrphanPolicy.WARN,
    OrphanPolicy.FAIL,
)
_ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)


def _assert_equals_oracle(shape: s.Shape, result: Any) -> None:
    oracle = run_full(shape).outputs
    assert set(result.outputs) == set(oracle)
    for table in oracle:
        assert sc.compare_strict(oracle[table], result.outputs[table]) == _ALL_TRUE, table


_CASES: list[tuple[str, Callable[[], s.Shape]]] = [
    ("two-preserve", lambda: two_table_cycle_shape(PRESERVE)),
    ("two-remap", lambda: two_table_cycle_shape(REMAP)),
    ("two-warn", lambda: two_table_cycle_shape(WARN)),
    ("three-preserve", lambda: three_table_cycle_shape(PRESERVE)),
    ("three-remap", lambda: three_table_cycle_shape(REMAP)),
    ("chain-preserve", lambda: chain_cycle_shape(PRESERVE)),
    ("chain-remap", lambda: chain_cycle_shape(REMAP)),
    ("mixed-preserve", lambda: mixed_topology_shape(PRESERVE)),
    ("mixed-remap", lambda: mixed_topology_shape(REMAP)),
    ("mixed-warn", lambda: mixed_topology_shape(WARN)),
]
_IDS = [c[0] for c in _CASES]


# Parity.


@pytest.mark.parametrize(("name", "build"), _CASES, ids=_IDS)
def test_seq_cycle_equals_full_frame_on_every_strict_dimension(
    name: str, build: Callable[[], s.Shape]
) -> None:
    shape = build()
    _assert_equals_oracle(shape, run_seq_cycle(shape))


@pytest.mark.parametrize(("name", "build"), _CASES, ids=_IDS)
def test_warnings_and_row_errors_equal_the_oracle_once(
    name: str, build: Callable[[], s.Shape]
) -> None:
    """Pass 1 is internal: its warnings must not be reported on top of pass 2's."""
    shape = build()

    def key(w: Any) -> tuple[Any, ...]:
        return (w.code, w.column, repr(sorted(w.detail.items())) if w.detail else "")

    assert sorted(map(key, run_seq_cycle(shape).warnings)) == sorted(
        map(key, run_full(shape).warnings)
    )


def test_ri_holds_in_both_directions_for_a_matched_cycle() -> None:
    shape = two_table_cycle_shape(
        a_id=["a1", "a2", "a3"], b_mid=["a1", "a2", "a3"], a_back=["a1", "a3", "a2"]
    )
    out = run_seq_cycle(shape).outputs
    a_id, b_mid, a_back = (set(out["A"]["id"].to_pylist()), out["B"]["mid"].to_pylist(), None)
    a_back_values = out["A"]["back"].to_pylist()
    assert set(b_mid) <= a_id
    assert set(a_back_values) <= set(b_mid)
    assert set(b_mid) == a_id and a_back is None


def test_the_transactional_sink_gets_every_table_and_one_commit() -> None:
    sink = RecordingSink()
    shape = mixed_topology_shape()
    result = run_seq_cycle(shape, sink=sink)
    assert result.outputs == {}
    assert sink.commits == 1 and sink.aborts == 0
    oracle = run_full(shape).outputs
    assert set(sink.committed) == set(oracle)
    for table in oracle:
        assert sc.compare_strict(oracle[table], sink.committed[table]) == _ALL_TRUE


# A range of key producers, not only hash.

_KEYS = ["key-001", "key-002", "key-003", "key-002"]
_DIGITS = ["10000001", "10000002", "10000003", "10000002"]


def _producers() -> list[tuple[str, Any, list[str], list[str], list[str]]]:
    def seed(strategy: str, **cfg: Any) -> Any:
        return column_seed(
            strategy,
            namespace=None if strategy in ("passthrough", "redact") else "ns_k",
            provider_config=tuple(cfg.items()),
        )

    mid = ["key-001", "key-002", "key-X99", "key-003"]
    back = ["key-001", "key-X99", "zzz-000", "key-003"]
    dmid = ["10000001", "10000002", "10000009", "10000003"]
    dback = ["10000001", "10000009", "10000008", "10000003"]
    return [
        ("hash", seed("hash"), _KEYS, mid, back),
        ("passthrough", seed("passthrough"), _KEYS, mid, back),
        ("redact", seed("redact"), _KEYS, mid, back),
        ("truncate", seed("truncate", length=5), _KEYS, mid, back),
        ("fpe", seed("fpe", charset="digits"), _DIGITS, dmid, dback),
    ]


@pytest.mark.parametrize(
    ("name", "key_seed", "a_id", "b_mid", "a_back"),
    _producers(),
    ids=[p[0] for p in _producers()],
)
def test_each_allowlisted_producer_matches_the_oracle(
    name: str, key_seed: Any, a_id: list[str], b_mid: list[str], a_back: list[str]
) -> None:
    shape = two_table_cycle_shape(PRESERVE, id_seed=key_seed, a_id=a_id, b_mid=b_mid, a_back=a_back)
    _assert_equals_oracle(shape, run_seq_cycle(shape))


def test_a_child_with_a_different_strategy_inherits_the_producers_value() -> None:
    redact = column_seed("redact", namespace=None)
    trunc = column_seed("truncate", namespace=None, provider_config=(("length", 1),))
    shape = two_table_cycle_shape(PRESERVE, back_seed=redact, mid_seed=trunc)
    out = run_seq_cycle(shape).outputs
    assert out["B"]["mid"].to_pylist()[0] == out["A"]["id"].to_pylist()[0]
    assert out["A"]["back"].to_pylist()[0] == out["B"]["mid"].to_pylist()[0]
    _assert_equals_oracle(shape, run_seq_cycle(shape))


# Canonicalization: the FK lookup treats True and 1 as one key.


def test_boolean_parent_matched_by_an_integer_child_inherits_the_parents_token() -> None:
    plan = s.make_plan(
        {
            "A": s.table_seed({"id": s.hashed("ns_ab"), "back": s.hashed("ns_ba")}),
            "B": s.table_seed({"mid": s.hashed("ns_ba")}),
        }
    )
    g = s.graph(
        s.edge("A", "id", "B", "mid", "ns_ab", PRESERVE),
        s.edge("B", "mid", "A", "back", "ns_ba", PRESERVE),
    )
    shape = s.Shape(
        plan,
        g,
        {
            "A": pa.table({"id": pa.array([True, False]), "back": pa.array([1, 0], pa.int64())}),
            "B": pa.table({"mid": pa.array([1, 0, 1], pa.int64())}),
        },
    )
    out = run_seq_cycle(shape).outputs
    assert out["B"]["mid"].to_pylist()[0] == out["A"]["id"].to_pylist()[0]
    assert out["B"]["mid"].to_pylist()[2] == out["A"]["id"].to_pylist()[0]
    _assert_equals_oracle(shape, run_seq_cycle(shape))


def test_duplicate_normalized_keys_resolve_last_wins_like_the_oracle() -> None:
    shape = two_table_cycle_shape(PRESERVE, a_id=["a1", "a1", "a2", "a1"])
    _assert_equals_oracle(shape, run_seq_cycle(shape))


# Orphans, cascade and policy.


def test_orphan_fail_raises_the_same_error_and_commits_nothing() -> None:
    shape = two_table_cycle_shape(FAIL)
    with pytest.raises(ExecutionError) as oracle:
        run_full(shape)
    sink = RecordingSink()
    with pytest.raises(ExecutionError) as seq:
        run_seq_cycle(shape, sink=sink)
    assert seq.value.code == oracle.value.code == "orphan_fk_violation"
    assert sink.commits == 0 and sink.aborts == 1 and sink.committed == {}


def test_orphan_preserved_at_the_middle_hop_stays_raw_and_the_next_hop_inherits_it() -> None:
    """The rev-1 chain: A.id -> B.mid -> A.back with `aX` preserved raw at B.mid."""
    shape = two_table_cycle_shape(PRESERVE)
    out = run_seq_cycle(shape).outputs
    assert out["B"]["mid"].to_pylist()[2] == "aX"
    assert out["A"]["back"].to_pylist()[1] == "aX"
    _assert_equals_oracle(shape, run_seq_cycle(shape))


def test_incoming_parent_with_a_false_when_stays_raw_and_the_cycle_inherits_raw() -> None:
    """The rev-1 `when:` counterexample, on a key outside the cycle."""
    shape = mixed_topology_shape(PRESERVE, p_key_when="gate == 'on'")
    out = run_seq_cycle(shape).outputs
    assert out["P"]["k"].to_pylist()[1] == "p2"
    assert out["A"]["id"].to_pylist()[1] == "p2"
    assert out["B"]["mid"].to_pylist()[1] == "p2"
    assert out["C"]["ref"].to_pylist()[1] == "p2"
    _assert_equals_oracle(shape, run_seq_cycle(shape))


def test_a_when_gated_key_inside_the_cycle_declines_not_misses() -> None:
    gated = dataclasses.replace(s.hashed("ns_ab"), when="pay != 'p0'")
    shape = two_table_cycle_shape(PRESERVE, id_seed=gated)
    with pytest.raises(ExecutionError) as caught:
        run_seq_cycle(shape)
    assert caught.value.code == "sequential_cycle_key_when_unsupported"


# Standalone producer versus consumer map (rev-4).


def test_a_producer_key_is_executed_from_originals_while_the_child_inherits_the_map() -> None:
    """-0.0 and 0.0 are one FK key, so the consumer map holds one last-wins entry. The
    producer column still emits its own per-row values."""
    trunc = column_seed("truncate", namespace=None, provider_config=(("length", 8),))
    plan = s.make_plan(
        {
            "A": s.table_seed({"id": trunc, "back": s.hashed("ns_ba")}),
            "B": s.table_seed({"mid": s.hashed("ns_ba")}),
        }
    )
    g = s.graph(
        s.edge("A", "id", "B", "mid", "ns_ab", PRESERVE),
        s.edge("B", "mid", "A", "back", "ns_ba", PRESERVE),
    )
    shape = s.Shape(
        plan,
        g,
        {
            "A": pa.table(
                {
                    "id": pa.array([-0.0, 0.0, 1.5], pa.float64()),
                    "back": pa.array([0.0, 1.5, 1.5], pa.float64()),
                }
            ),
            "B": pa.table({"mid": pa.array([-0.0, 0.0, 1.5], pa.float64())}),
        },
    )
    out = run_seq_cycle(shape).outputs
    producer = out["A"]["id"].to_pylist()
    consumer = out["B"]["mid"].to_pylist()
    assert producer[0] != producer[1]  # the producer column keeps its own per-row output
    assert consumer[0] == consumer[1] == producer[1]  # the child inherits the last-wins entry
    _assert_equals_oracle(shape, run_seq_cycle(shape))


# The two passes: narrow read first, whole tables second.


def test_pass_one_reads_only_the_key_columns_before_any_whole_table_load() -> None:
    shape = mixed_topology_shape()
    events: list[tuple[str, Any]] = []
    calls: list[tuple[str, tuple[str, ...]]] = []
    ctx = cycle_ctx(shape, calls=calls)
    loads: list[str] = []
    run_seq_cycle(shape, ctx=ctx, loads=loads)
    assert sorted(calls) == [("A", ("back", "id")), ("B", ("mid",))]
    assert sorted(loads) == ["A", "B", "C", "D", "P"]
    assert events == []


def test_the_projected_read_precedes_the_first_cyclic_table_load() -> None:
    shape = two_table_cycle_shape()
    order: list[str] = []
    base = cycle_ctx(shape)
    inner = base.projected_loader
    assert inner is not None

    def spy(table: str, cols: Any) -> pa.Table:
        order.append(f"narrow:{table}")
        return inner(table, cols)

    ctx = dataclasses.replace(base, projected_loader=spy)
    loads: list[str] = []
    srcs = shape.sources

    def loader(table: str) -> pa.Table:
        order.append(f"whole:{table}")
        loads.append(table)
        return srcs[table]

    from decoy_engine.execution import PandasExecutionAdapter
    from tests.parity._d2b_shapes import NAMESPACES, REGISTRY

    _sequential.run_sequential(
        PandasExecutionAdapter(),
        shape.plan,
        loader,
        registry=REGISTRY,
        relationship_graph=shape.graph,
        namespace_registry=NAMESPACES,
        cycle_context=ctx,
    )
    assert order[:2] == ["narrow:A", "narrow:B"]
    assert [e for e in order if e.startswith("whole:")] == ["whole:A", "whole:B"]


def test_a_loader_with_no_projected_read_declines_rather_than_reading_whole_frames() -> None:
    shape = two_table_cycle_shape()
    loads: list[str] = []
    with pytest.raises(ExecutionError) as caught:
        run_seq_cycle(shape, ctx=cycle_ctx(shape, projected=False), loads=loads)
    assert caught.value.code == PROJECTED_READ
    assert loads == []


# Map release: after the last consumer across both passes and outgoing children.


def test_each_map_is_released_after_its_last_consumer_and_nothing_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = mixed_topology_shape()
    seen: list[tuple[str, frozenset[tuple[str, tuple[str, ...]]]]] = []
    real = _sequential_cycle.release_parent_maps

    def spy(graph: Any, table: str, remaining: Any, parent_map_cache: Any, errored: Any) -> None:
        seen.append((table, frozenset(parent_map_cache)))
        real(graph, table, remaining, parent_map_cache, errored)
        seen.append((f"after:{table}", frozenset(parent_map_cache)))

    monkeypatch.setattr(_sequential_cycle, "release_parent_maps", spy)
    monkeypatch.setattr(_sequential, "release_parent_maps", spy)
    run_seq_cycle(shape)
    live = dict(seen)
    incoming, forward, back = ("P", ("k",)), ("A", ("id",)), ("B", ("mid",))
    tables = [t for t, _ in seen if not t.startswith("after:")]
    # Pass 1 built the cyclic maps before the first cyclic table finished.
    assert {incoming, forward, back} <= live[tables[tables.index("A")]]
    # A.id feeds B only: released once B is done, not after A.
    assert forward in live["after:A"] and forward not in live["after:B"]
    # B.mid feeds A.back (a back-edge, consumed when A ran) and C.ref (an outgoing child):
    # it must outlive A and B and go once C is done.
    assert back in live["after:A"] and back in live["after:B"]
    assert back not in live["after:C"]
    # The incoming map P.k feeds A only.
    assert incoming not in live["after:A"]
    assert live[f"after:{tables[-1]}"] == frozenset()


# Declines at the SEQ entry point (fail closed, coded, no data).


def test_quarantine_triggers_decline_the_cycle_at_the_entry_point() -> None:
    shape = two_table_cycle_shape()
    q = {"enabled": True, "triggers": ["mask_error"], "output_path": "q.jsonl"}
    with pytest.raises(ExecutionError) as caught:
        run_seq_cycle(shape, quarantine=q)
    assert caught.value.code == QUARANTINE


def test_a_non_allowlisted_producer_declines_at_the_entry_point() -> None:
    shape = two_table_cycle_shape(id_seed=column_seed("faker", namespace="ns_k"))
    with pytest.raises(ExecutionError) as caught:
        run_seq_cycle(shape)
    assert caught.value.code == KEY_STRATEGY
    for leaked in ("a1", "aX", "zz"):
        assert leaked not in str(caught.value)


def test_a_joint_mask_writer_of_a_key_declines_at_the_entry_point() -> None:
    jm = column_seed(
        "joint_mask",
        namespace="ns_jm",
        provider_config=(("columns", ("id", "zip")), ("reference", "zips"), ("key_by", "pay")),
    )
    shape = two_table_cycle_shape(extra_a={"zip": (jm, pa.array(["z1", "z2", "z3", "z4"]))})
    with pytest.raises(ExecutionError) as caught:
        run_seq_cycle(shape)
    assert caught.value.code == KEY_WRITER


def test_the_combined_budget_declines_before_any_table_is_read() -> None:
    shape = mixed_topology_shape()
    ctx = dataclasses.replace(
        cycle_ctx(shape, budget=3 * 1024**3),
        table_rows=dict.fromkeys("PABCD", 5_000_000),
        table_bytes=dict.fromkeys("PABCD", 1_000),
    )
    loads: list[str] = []
    sink = RecordingSink()
    with pytest.raises(ExecutionError) as caught:
        run_seq_cycle(shape, ctx=ctx, loads=loads, sink=sink)
    assert caught.value.code == BUDGET_EXCEEDED
    assert loads == [] and sink.staged == {} and sink.commits == 0


def test_a_cycle_with_no_context_declines_instead_of_raising_a_raw_relationship_cycle() -> None:
    from decoy_engine.execution import PandasExecutionAdapter
    from tests.parity._d2b_shapes import NAMESPACES, REGISTRY

    shape = two_table_cycle_shape()
    with pytest.raises(ExecutionError) as caught:
        _sequential.run_sequential(
            PandasExecutionAdapter(),
            shape.plan,
            lambda t: shape.sources[t],
            registry=REGISTRY,
            relationship_graph=shape.graph,
            namespace_registry=NAMESPACES,
        )
    assert caught.value.code == "sequential_cycle_budget_unavailable"


def test_self_reference_and_acyclic_shapes_still_run_without_a_context() -> None:
    for shape in (s.self_ref_shape(), s.chain_shape(), s.diamond_shape()):
        result = s.run_seq(shape)
        oracle = s.run_full(shape).outputs
        for table in oracle:
            assert sc.compare_strict(oracle[table], result.outputs[table]) == _ALL_TRUE


def test_pass_one_row_errors_fail_loud_like_the_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key row that errors during pass 1 surfaces as the same fail-loud error."""
    from decoy_engine.execution._row_errors import RowError

    shape = two_table_cycle_shape()
    real = _sequential_cycle.drain_row_errors

    def inject(rows: Any, *, table: str) -> Any:
        batch = real(rows, table=table)
        if table == "A" and not batch:
            from decoy_engine.execution._row_errors import RowErrorRecord

            return [
                RowErrorRecord(
                    table="A", column="id", row_index=0, trigger="mask_error", reason="injected"
                )
            ]
        return batch

    monkeypatch.setattr(_sequential_cycle, "drain_row_errors", inject)
    assert RowError  # imported for the record type's module path
    with pytest.raises(RowErrorsFailedError):
        run_seq_cycle(shape)


# The stand-in kernel: pass 1 and pass 2 both hash on the kernel, and the output carries its mark.


def test_kernel_backed_producers_are_hashed_on_the_kernel_in_both_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = two_table_cycle_shape(PRESERVE)
    oracle = run_full(shape).outputs
    kernel = install(monkeypatch, MarkingKernel())
    python_rows = spy_python_hash(monkeypatch)
    out = run_seq_cycle(shape).outputs
    assert python_rows == []
    for table, expected in oracle.items():
        for column in expected.column_names:
            marked = [
                MARK + v if isinstance(v, str) and len(v) == 64 else v
                for v in expected[column].to_pylist()
            ]
            assert out[table][column].to_pylist() == marked, (table, column)
    narrow = [c for c in kernel.calls if c.namespace == "ns_ab"]
    assert len(narrow) == 2  # once for the key-only read, once for the full table


def test_a_no_kernel_producer_is_proven_by_parity_not_the_kernel_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, MarkingKernel())
    trunc = column_seed("truncate", namespace=None, provider_config=(("length", 5),))
    shape = two_table_cycle_shape(
        PRESERVE,
        id_seed=trunc,
        a_id=_KEYS,
        b_mid=["key-001", "key-002", "key-X99", None],
        a_back=["key-001", "key-X99", "zzz-000", None],
    )
    out = run_seq_cycle(shape).outputs
    assert all(c.namespace != "ns_k" for c in kernel.calls)
    oracle = run_full(shape).outputs
    assert out["A"]["id"].to_pylist() == oracle["A"]["id"].to_pylist()
