"""Phase D / Dmp: multi-parent FK admitted on the out-of-core route, strictly against the oracle.

One child column tuple with edges to N parents resolves first-hit-wins in `graph.edges`
order on the full-frame oracle and the sequential route; this file pins the out-of-core
route to the same output (every strict dimension but the D0-G1 metadata key), the same
warning object, the same FAIL code, and the same precedence, with per-parent tokens that
differ so a reversed precedence is visible. The admission cases fail on a gate that still
declines the shape and pass once the merged first-hit-wins relation lands.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile._types import Profile, Relationship
from decoy_engine.relationships import build_namespace_registry, build_relationship_graph
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dmp_shapes as m
from tests.parity._d1a_hash_data import PythonReferenceKernel, install_companion
from tests.parity._kernel_witness import ExpectedMask, KernelWitness, force_companion_unavailable

P, R, W, F = OrphanPolicy.PRESERVE, OrphanPolicy.REMAP, OrphanPolicy.WARN, OrphanPolicy.FAIL
POLICIES = [P, R, W]
MULTI_PARENT_WARNING = (
    "orphan_fk",
    "ns_x",
    "ref",
    {
        "parent_table": "P1",
        "parent_columns": ["k"],
        "child_table": "X",
        "child_columns": ["ref"],
        "orphan_rows": 1,
    },
)


def _gate_codes(shape: s.Shape) -> list[str]:
    work = order_work(build_work_list(shape.plan, s.REGISTRY), shape.graph)
    return [
        r.code for r in check_out_of_core_compatibility(shape.plan, work, shape.graph).rejections
    ]


def _agree(shape: s.Shape, **kw: object) -> tuple[Any, m.OocRun]:
    """Both routes succeed and differ on the oracle's `pandas` schema key alone (D0-G1)."""
    oracle = s.run_full(shape)
    run = m.run_ooc(shape, **kw)  # type: ignore[arg-type]
    m.assert_agrees(oracle, run)
    assert m.sorted_warnings(run.warnings) == m.sorted_warnings(oracle.warnings)
    return oracle, run


def _tokens(run: m.OocRun, table: str, column: str = "k") -> list[object]:
    return run.outputs[table].column(column).to_pylist()


# M1 to M4: the two-parent shape with an overlapping key.


def test_gate_admits_a_multi_parent_child() -> None:
    assert _gate_codes(s.multi_parent_shape(P)) == []
    assert _gate_codes(m.composite_multi_parent_shape(R)) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_m1_m3_first_declared_parent_wins_and_the_oracle_agrees(policy: OrphanPolicy) -> None:
    oracle, run = _agree(s.multi_parent_shape(policy))
    p1, p2 = _tokens(run, "P1"), _tokens(run, "P2")
    assert p1[1] != p2[1]  # the shared key has a distinguishable token per parent
    ref = _tokens(run, "X", "ref")
    assert ref[:3] == [p1[0], p2[0], p1[1]]
    if policy is R:
        assert ref[3] not in ("nowhere", None) and len(ref[3]) == 64
    else:
        assert ref[3] == "nowhere"
    expected = [MULTI_PARENT_WARNING] if policy is W else []
    assert m.warning_keys(run.warnings) == expected


def test_m4_fail_raises_the_orphan_code_naming_the_first_parent() -> None:
    shape = s.multi_parent_shape(F)
    oracle = m.raised(lambda: s.run_full(shape))
    ooc = m.raised(lambda: m.run_ooc(shape))
    assert oracle[:2] == ooc[:2] == (ExecutionError, m.ORPHAN_CODE)
    # The oracle's message names the FIRST edge's parent; byte-identity of the two routes'
    # messages is the Dcomp-OOC contract (one producer for the error), pinned there.
    assert "P1.('k',)" in oracle[2] and "P2" not in oracle[2]
    assert "X.('ref',)" in ooc[2]


# M5 and M6: the precedence is the edge order, not a table-name order or an accident.


def test_m5_reversing_the_declared_order_moves_the_shared_key_to_the_other_parent() -> None:
    forward = m.run_ooc(s.multi_parent_shape(P))
    reversed_shape = m.reversed_shape(s.multi_parent_shape(P))
    oracle, backward = _agree(reversed_shape)
    assert _tokens(backward, "X", "ref")[2] == _tokens(backward, "P2")[1]
    assert _tokens(forward, "X", "ref")[2] == _tokens(forward, "P1")[1]
    assert _tokens(forward, "X", "ref")[2] != _tokens(backward, "X", "ref")[2]


@pytest.mark.parametrize("policy", POLICIES)
def test_m6_three_parents_resolve_by_rank(policy: OrphanPolicy) -> None:
    oracle, run = _agree(m.three_parent_shape(policy))
    p1, p2, p3 = (_tokens(run, t) for t in ("P1", "P2", "P3"))
    ref = _tokens(run, "X", "ref")
    assert ref[:6] == [p1[0], p2[0], p3[0], p1[1], p2[2], None]
    assert len({p1[1], p2[1], p3[1]}) == 3  # `all3` has three different tokens


# M7 to M10: composite tuples, integer keys, batch boundaries, lanes and threads.


@pytest.mark.parametrize("policy", POLICIES)
def test_m7_composite_multi_parent_tuple(policy: OrphanPolicy) -> None:
    oracle, run = _agree(m.composite_multi_parent_shape(policy))
    c_k, c_j = _tokens(run, "C", "k"), _tokens(run, "C", "j")
    assert (c_k[2], c_j[2]) == (_tokens(run, "P1", "k")[1], _tokens(run, "P1", "j")[1])
    assert c_k[4] is None and c_j[4] is None


@pytest.mark.parametrize("policy", POLICIES)
def test_m8_integer_passthrough_parents(policy: OrphanPolicy) -> None:
    oracle, run = _agree(m.int_passthrough_shape(policy))
    assert run.outputs["X"].schema.field("ref").type == pa.int64()
    assert _tokens(run, "X", "ref")[:3] == [1, 2, 3] and _tokens(run, "X", "ref")[4] is None


@pytest.mark.parametrize("batch_rows", [1, 2, None], ids=["rows1", "rows2", "default"])
@pytest.mark.parametrize("policy", [P, R])
def test_m9_overlapping_keys_across_batch_boundaries_on_both_lanes(
    policy: OrphanPolicy, batch_rows: int | None, tmp_path: Path
) -> None:
    shape = m.wide_shape(policy, rows=6)
    oracle, resident = _agree(shape, batch_rows=batch_rows)
    batch = m.run_ooc(shape, "sink-batch", tmp_path=tmp_path / "b", batch_rows=batch_rows)
    reorder = m.run_ooc(shape, "sink-reorder", tmp_path=tmp_path / "r", batch_rows=batch_rows)
    m.assert_lane(batch, "sink-batch")
    m.assert_lane(reorder, "sink-reorder")
    for table in shape.sources:
        assert reorder.outputs[table].equals(batch.outputs[table], check_metadata=True), table
    for run in (batch, reorder):
        m.assert_agrees(oracle, run)
        assert m.sorted_warnings(run.warnings) == m.sorted_warnings(oracle.warnings)
    # Every matched child key is P2's token only where P1 lacks the key (odd keys).
    p1 = dict(
        zip(shape.sources["P1"].column("k").to_pylist(), _tokens(resident, "P1"), strict=True)
    )
    p2 = dict(
        zip(shape.sources["P2"].column("k").to_pylist(), _tokens(resident, "P2"), strict=True)
    )
    for src, out in zip(
        shape.sources["X"].column("ref").to_pylist(), _tokens(resident, "X", "ref"), strict=True
    ):
        if src in p1:
            assert out == p1[src]
        elif src in p2:
            assert out == p2[src]


class _ThreadRecorder(PythonReferenceKernel):
    """The stand-in, recording the `native_threads` each `derive_batch` call received."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: set[int | None] = set()

    def derive_batch(self, values: Any, *, native_threads: int | None = None, **kw: Any) -> Any:
        self.threads.add(native_threads)
        return super().derive_batch(values, native_threads=native_threads, **kw)


def test_m10_native_threads_reach_the_kernel_and_the_stand_in_output_is_thread_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plumbing only: the stand-in ignores the thread count, so this proves `native_threads`
    reaches every `derive_batch` call (for every parent) and the route's output does not depend
    on it. Thread invariance of the COMPILED kernel's tokens on these shapes is
    `test_phase_d_dmp_multi_parent_ooc_real.py`."""
    shape = m.three_parent_shape(R)
    runs: dict[int, m.OocRun] = {}
    for threads in (1, 4):
        kernel = _ThreadRecorder()
        install_companion(monkeypatch, kernel)
        runs[threads] = m.run_ooc(shape, native_threads=threads)
        assert kernel.threads == {threads}
    for table in shape.sources:
        assert runs[4].outputs[table].equals(runs[1].outputs[table], check_metadata=True), table
    m.assert_agrees(s.run_full(shape), runs[4])


# Contract controls: the metadata exemption is exactly the oracle's `pandas` schema key, on the
# multi-parent child too.

_FIELD_METADATA_ONLY = {**m.ALL_TRUE, "field_metadata": False}
_UNEXPECTED = {b"decoy_unexpected": b"1"}


def _with_outputs(run: m.OocRun, **tables: pa.Table) -> m.OocRun:
    return dataclasses.replace(run, outputs={**run.outputs, **tables})


def test_the_metadata_contract_rejects_unexpected_metadata_on_the_multi_parent_child() -> None:
    """Negative controls: per-field metadata on `X.ref` and a schema key other than the
    oracle's `pandas` (either side) each fail `assert_agrees`; the raw report alone is blind to
    the first (the dimension reads False either way)."""
    oracle, run = _agree(s.multi_parent_shape(P))
    table = run.outputs["X"]
    idx = table.schema.get_field_index("ref")
    schema = table.schema.set(idx, table.schema.field(idx).with_metadata(_UNEXPECTED))
    field_injected = _with_outputs(run, X=pa.Table.from_arrays(table.columns, schema=schema))
    assert m.strict(oracle, field_injected)["X"] == m.METADATA_ONLY
    assert m.strict_modulo_pandas(oracle, field_injected)["X"] == _FIELD_METADATA_ONLY
    with pytest.raises(AssertionError):
        m.assert_agrees(oracle, field_injected)
    schema_injected = _with_outputs(run, X=table.replace_schema_metadata(_UNEXPECTED))
    assert m.strict_modulo_pandas(oracle, schema_injected)["X"] == _FIELD_METADATA_ONLY
    with pytest.raises(AssertionError):
        m.assert_agrees(oracle, schema_injected)
    child = oracle.outputs["X"]
    extra = child.replace_schema_metadata({**child.schema.metadata, **_UNEXPECTED})
    oracle_extra = SimpleNamespace(outputs={**oracle.outputs, "X": extra}, warnings=oracle.warnings)
    assert m.strict_modulo_pandas(oracle_extra, run)["X"] == _FIELD_METADATA_ONLY
    with pytest.raises(AssertionError):
        m.assert_agrees(oracle_extra, run)


# M11 and M12: the declared-policy agreement rule and the one shape the merge refuses.


def _compiled_graph(first: OrphanPolicy, second: OrphanPolicy) -> Any:
    """The planner's path (`build_relationship_graph`), which owns `orphan_policy_conflict`."""
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
        profiled_at=datetime(2026, 10, 24),
        decoy_engine_version="0.1.0",
    )
    registry = build_namespace_registry({"tables": []}, profile)
    lookup = {("P1", ("k",), "X", ("ref",)): first, ("P2", ("k",), "X", ("ref",)): second}
    return build_relationship_graph(rels, namespace_registry=registry, orphan_policy_lookup=lookup)


@pytest.mark.parametrize(("first", "second"), [(P, R), (R, F), (W, P)])
def test_m12_conflicting_orphan_policies_on_one_child_tuple_fail_at_graph_build_and_at_the_merge(
    first: OrphanPolicy, second: OrphanPolicy, tmp_path: Path
) -> None:
    """Per-edge policy agreement is the planner's rule on every route (`orphan_policy_conflict`
    at graph build); the out-of-core merge re-checks it under the same code, so a graph that
    bypassed the planner is refused before any output instead of resolved under the first
    edge's policy. Agreeing policies build and run."""
    with pytest.raises(PlanCompileError) as exc:
        _compiled_graph(first, second)
    assert exc.value.code == "orphan_policy_conflict"
    assert len(_compiled_graph(first, first).edges) == 2
    base = s.multi_parent_shape(first)
    bypass = s.Shape(
        base.plan,
        s.graph(
            s.edge("P1", "k", "X", "ref", "ns_x", first),
            s.edge("P2", "k", "X", "ref", "ns_x", second),
        ),
        base.sources,
    )
    assert _gate_codes(bypass) == []  # the static gate does not own this rule
    kind, code, message = m.raised(lambda: m.run_ooc(bypass))
    assert (kind, code) == (ExecutionError, "orphan_policy_conflict")
    assert "X.('ref',)" in message and first.value in message and second.value in message
    kind, code, _ = m.raised(lambda: m.run_ooc(bypass, "sink-batch", tmp_path=tmp_path))
    assert (kind, code) == (ExecutionError, "orphan_policy_conflict")
    assert not list((tmp_path / "out").rglob("*.parquet"))


@pytest.mark.parametrize("policy", POLICIES)
def test_m11_heterogeneous_masked_key_types_fail_closed_before_any_output(
    policy: OrphanPolicy, tmp_path: Path
) -> None:
    shape = m.int_passthrough_shape(policy, p2_type=pa.float64())
    assert s.run_full(shape).outputs["X"].num_rows == 5  # the oracle types the column by value
    kind, code, message = m.raised(lambda: m.run_ooc(shape))
    assert (kind, code) == (ExecutionError, m.FK_DTYPE_CODE)
    # The reason names the two masked key types and the route to take, and nothing else: no
    # table or column identifiers and no key values reach the message.
    assert "the parents' masked key types differ (int64 vs double)" in message
    assert "execution_mode='sequential'" in message
    assert "P1" not in message and "P2" not in message and "ref" not in message
    kind, code, _ = m.raised(lambda: m.run_ooc(shape, "sink-batch", tmp_path=tmp_path))
    assert (kind, code) == (ExecutionError, m.FK_DTYPE_CODE)
    assert not list((tmp_path / "out").rglob("*.parquet"))  # the sink aborted, nothing published


@pytest.mark.parametrize("policy", POLICIES)
def test_m11_routing_keeps_the_heterogeneous_shape_off_out_of_core(policy: OrphanPolicy) -> None:
    """The static gate cannot see dtypes, but the router's schema-aware hazard can: differing
    masked key types across the parents of one child tuple divert the job to the sequential
    route before any parent is written, while same-typed parents stay out-of-core-eligible."""
    from decoy_engine.execution._pipeline_routing_fk_dtype import fk_out_of_core_dtype_hazard

    mixed = m.int_passthrough_shape(policy, p2_type=pa.float64())
    same = m.int_passthrough_shape(policy)
    assert fk_out_of_core_dtype_hazard(mixed.plan, mixed.graph, caller_sources=mixed.sources)
    assert not fk_out_of_core_dtype_hazard(same.plan, same.graph, caller_sources=same.sources)


# M14: compute provenance (stand-in companion; the compiled kernel is the `_real` suite).

_PARENTS = [ExpectedMask("P1", "k", "hash_array"), ExpectedMask("P2", "k", "hash_array")]
_EVIDENCE_REMAP = [
    {"table": "P1", "column": "k", "path": "mask_batch", "executed_backend": "rust_companion"},
    {"table": "P2", "column": "k", "path": "mask_batch", "executed_backend": "rust_companion"},
    {
        "table": "X",
        "column": "ref",
        "path": "fk_remap",
        "executed_backend": "python_scalar",
        "reason": "fk_key_hash_remap_native_deferred:batch_join",
    },
]


@pytest.mark.parametrize("mode", m.MODES)
def test_m14_both_parents_keys_are_native_and_the_merged_relation_masks_nothing(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    shape = s.multi_parent_shape(P)
    witness = KernelWitness()
    with witness.observe():
        run = m.run_ooc(shape, mode, tmp_path=tmp_path, batch_rows=1)
    witness.assert_covered(shape.sources, _PARENTS)
    witness.assert_native_ran(shape.sources, run.outputs, _PARENTS)
    assert not any(obs.table == "X" for obs in witness.observations)
    assert not any(obs.table is None for obs in witness.observations)


def test_m14_evidence_names_both_native_parents_and_the_first_parents_remap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_companion(monkeypatch)
    assert m.hash_evidence(m.run_ooc(s.multi_parent_shape(R))) == _EVIDENCE_REMAP


def test_m14_without_the_companion_both_parents_run_python(monkeypatch: pytest.MonkeyPatch) -> None:
    force_companion_unavailable(monkeypatch)
    shape = s.multi_parent_shape(P)
    witness = KernelWitness()
    with witness.observe():
        m.run_ooc(shape)
    witness.assert_legacy_path_only(shape.sources, _PARENTS)
