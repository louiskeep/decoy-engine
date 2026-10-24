"""Phase D / Dmp: multi-parent FK admitted on the out-of-core route, strictly against the oracle.

One child column tuple with edges to N parents resolves first-hit-wins in `graph.edges`
order on the full-frame oracle and the sequential route; this file pins the out-of-core
route to the same output (every strict dimension but the D0-G1 metadata key), the same
warning object, the same FAIL code, and the same precedence, with per-parent tokens that
differ so a reversed precedence is visible. The admission cases fail on a gate that still
declines the shape and pass once the merged first-hit-wins relation lands.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dmp_shapes as m
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import install_companion
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


def _agree(shape: s.Shape, **kw: object) -> tuple[object, m.OocRun]:
    oracle = s.run_full(shape)
    run = m.run_ooc(shape, **kw)  # type: ignore[arg-type]
    assert m.strict(oracle, run) == {t: m.METADATA_ONLY for t in oracle.outputs}
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
    assert (batch.reorder_joiners, reorder.batch_joiners) == (0, 0) and reorder.reorder_joiners > 0
    for table in shape.sources:
        assert reorder.outputs[table].equals(batch.outputs[table], check_metadata=True), table
        assert sc.compare_strict(batch.outputs[table], reorder.outputs[table]) == m.ALL_TRUE
    assert m.strict(oracle, batch) == {t: m.METADATA_ONLY for t in shape.sources}
    assert m.sorted_warnings(batch.warnings) == m.sorted_warnings(oracle.warnings)
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


def test_m10_thread_count_invariance(monkeypatch: pytest.MonkeyPatch) -> None:
    install_companion(monkeypatch)
    shape = m.three_parent_shape(R)
    one, four = m.run_ooc(shape, native_threads=1), m.run_ooc(shape, native_threads=4)
    for table in shape.sources:
        assert four.outputs[table].equals(one.outputs[table], check_metadata=True), table


# M11: the one shape the merge refuses.


@pytest.mark.parametrize("policy", POLICIES)
def test_m11_heterogeneous_masked_key_types_fail_closed_before_any_output(
    policy: OrphanPolicy, tmp_path: Path
) -> None:
    shape = m.int_passthrough_shape(policy, p2_type=pa.float64())
    assert s.run_full(shape).outputs["X"].num_rows == 5  # the oracle types the column by value
    assert m.raised(lambda: m.run_ooc(shape))[:2] == (ExecutionError, m.FK_DTYPE_CODE)
    kind, code, _ = m.raised(lambda: m.run_ooc(shape, "sink-batch", tmp_path=tmp_path))
    assert (kind, code) == (ExecutionError, m.FK_DTYPE_CODE)
    assert not list((tmp_path / "out").rglob("*.parquet"))  # the sink aborted, nothing published


# M14: compute provenance.

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
