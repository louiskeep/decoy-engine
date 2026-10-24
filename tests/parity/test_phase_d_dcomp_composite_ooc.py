"""Phase D / Dcomp-OOC: composite FK on the out-of-core route, strictly against the oracle.

The value-normalized harness proved the canonical `composite_fk_group` shape is value-equal
out-of-core; this file pins the strict contract (plan section 4): every dimension but
`field_metadata` equal, the warning object identical, the FAIL raise identical in code and
message, and the symmetric fail-closed codes. Every expectation is a literal. Where the two
routes differ today for a reason outside this slice (plan section 5), the exact delta is
pinned with a scalar single-column control beside it, so a later slice can neither widen it
nor mistake it for a composite problem.

Resident path only; the sink path and the reorder lane are `test_phase_d_dcomp_composite_ooc_sink.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.execution._pipeline_routing_signals import out_of_core_admission
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dcomp_ooc_shapes as o
from tests.parity import _dcomp_shapes as d
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import install_companion
from tests.parity._kernel_witness import force_companion_unavailable
from tests.parity.test_out_of_core_fk_parity import _build_single_edge, _run_ooc, _run_oracle

P, R, W, F = OrphanPolicy.PRESERVE, OrphanPolicy.REMAP, OrphanPolicy.WARN, OrphanPolicy.FAIL
POLICIES = [P, R, W]
COMPOSITE_FAIL_MSG = (
    "1 orphan row(s) in C.('k', 'j') reference no parent key in P.('k', 'j') (orphan_policy=fail)."
)
COMPOSITE_WARNING = (
    "orphan_fk",
    "ns_cg",
    "k,j",
    {
        "parent_table": "P",
        "parent_columns": ["k", "j"],
        "child_table": "C",
        "child_columns": ["k", "j"],
        "orphan_rows": 1,
    },
)
CHAIN_CODE = "out_of_core_composite_fk_chain_unsupported"
SCALAR_CHILD_CODE = "out_of_core_composite_fk_scalar_child_unsupported"


def _agree(shape: s.Shape, **kw: Any) -> tuple[Any, o.OocRun]:
    """Both routes succeed and differ on `field_metadata` alone, by the D0-G1 literal."""
    oracle = s.run_full(shape)
    run = o.run_ooc(shape, **kw)
    assert o.strict(oracle, run) == {t: o.METADATA_ONLY for t in oracle.outputs}
    for t in oracle.outputs:
        assert sc.metadata_keys(oracle.outputs[t]) == o.ORACLE_META, t
        assert sc.metadata_keys(run.outputs[t]) == o.OOC_META, t
    # The same warning objects; their cross-table ORDER is route-dependent (G9 below).
    assert o.sorted_warnings(run.warnings) == o.sorted_warnings(oracle.warnings)
    return oracle, run


def _signatures(oracle: Any, run: o.OocRun, table: str) -> tuple[str, str]:
    return sc.schema_signature(oracle.outputs[table]), sc.schema_signature(run.outputs[table])


def _gate_codes(shape: s.Shape) -> list[str]:
    work = order_work(build_work_list(shape.plan, s.REGISTRY), shape.graph)
    return [
        r.code for r in check_out_of_core_compatibility(shape.plan, work, shape.graph).rejections
    ]


# O1 to O3: the canonical group shape.


@pytest.mark.parametrize("policy", POLICIES)
def test_o1_matched_orphan_and_partial_null_tuples_agree_on_every_dimension_but_metadata(
    policy: OrphanPolicy,
) -> None:
    oracle, run = _agree(s.composite_shape(policy))
    expected = [COMPOSITE_WARNING] if policy is W else []
    assert o.warning_keys(run.warnings) == expected
    assert o.warning_keys(oracle.warnings) == expected
    # The partial-null tuple (row 3) is null on every component on both routes, never a
    # preserved non-null component.
    assert run.outputs["C"].column("k").to_pylist()[3] is None
    assert run.outputs["C"].column("j").to_pylist()[3] is None


def test_o2_fail_with_a_genuine_orphan_raises_the_oracles_code_and_message_on_both_routes() -> None:
    shape = s.composite_shape(F)
    oracle = o.raised(lambda: s.run_full(shape))
    ooc = o.raised(lambda: o.run_ooc(shape))
    assert oracle == (ExecutionError, o.ORPHAN_CODE, f"[{o.ORPHAN_CODE}] {COMPOSITE_FAIL_MSG}")
    assert ooc == oracle


@pytest.mark.parametrize("batch_rows", [1, 2, 3, None], ids=["rows1", "rows2", "rows3", "default"])
@pytest.mark.parametrize("policy", [P, R])
def test_o3_output_is_independent_of_the_batch_size(
    policy: OrphanPolicy, batch_rows: int | None
) -> None:
    _agree(s.composite_shape(policy), batch_rows=batch_rows)


def test_o3_output_is_independent_of_the_native_thread_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_companion(monkeypatch)
    shape = s.composite_shape(R)
    one = o.run_ooc(shape, native_threads=1)
    four = o.run_ooc(shape, native_threads=4)
    for t in shape.sources:
        assert four.outputs[t].equals(one.outputs[t], check_metadata=True), t
    assert o.strict(s.run_full(shape), four) == {t: o.METADATA_ONLY for t in shape.sources}


# O4 to O10: one key component per admitted dtype beside a hashed string component.


@pytest.mark.parametrize("policy", POLICIES)
def test_o4_null_bearing_int64_component_keeps_its_integer_type(policy: OrphanPolicy) -> None:
    oracle, run = _agree(
        d.int_component_shape(
            policy, parent_n=[1, None, 3], child_k=["a", "c", "q", "a"], child_n=[1, 3, 9, None]
        )
    )
    assert _signatures(oracle, run, "C") == ("k:string,n:int64", "k:string,n:int64")


@pytest.mark.parametrize("policy", POLICIES)
def test_o5_int64_component_beyond_float_precision_stays_exact(policy: OrphanPolicy) -> None:
    oracle, run = _agree(
        d.int_component_shape(
            policy, parent_n=[d.BIG, 2, 3], child_k=["a", "q", None], child_n=[d.BIG, d.BIG + 1, 5]
        )
    )
    assert run.outputs["C"].column("n").to_pylist() == [d.BIG, d.BIG + 1, None]
    assert _signatures(oracle, run, "C") == ("k:string,n:int64", "k:string,n:int64")


@pytest.mark.parametrize("n_type", [pa.int32(), pa.int16()], ids=str)
@pytest.mark.parametrize("policy", POLICIES)
def test_o6_narrow_integer_component_keeps_the_parent_width_and_widens_the_child_on_both_routes(
    policy: OrphanPolicy, n_type: pa.DataType
) -> None:
    oracle, run = _agree(o.narrow_int_component_shape(policy, n_type))
    assert _signatures(oracle, run, "P") == (f"k:string,n:{n_type}", f"k:string,n:{n_type}")
    assert _signatures(oracle, run, "C") == ("k:string,n:int64", "k:string,n:int64")


@pytest.mark.parametrize("orphan_n", [5.0, 2.5], ids=["whole", "fractional"])
@pytest.mark.parametrize("policy", POLICIES)
def test_o7_float_component_with_a_representable_orphan(
    policy: OrphanPolicy, orphan_n: float
) -> None:
    oracle, run = _agree(d.float_int_orphan_shape(policy, orphan_n=orphan_n))
    assert _signatures(oracle, run, "C") == ("k:string,n:double", "k:string,n:double")


@pytest.mark.parametrize("policy", POLICIES)
def test_o8_whole_float_orphan_beyond_float_precision_fails_closed_on_both_routes(
    policy: OrphanPolicy,
) -> None:
    shape = d.float_int_orphan_shape(policy, orphan_n=float(2**62))
    assert o.raised(lambda: s.run_full(shape))[:2] == (ExecutionError, o.FK_DTYPE_CODE)
    assert o.raised(lambda: o.run_ooc(shape))[:2] == (ExecutionError, o.FK_DTYPE_CODE)


@pytest.mark.parametrize("policy", POLICIES)
def test_o9_float_parent_with_a_non_exact_integer_orphan_fails_closed_on_both_routes(
    policy: OrphanPolicy,
) -> None:
    shape = d.float_parent_int_child_shape(policy, orphan_int=2**53 + 1)
    assert o.raised(lambda: s.run_full(shape))[:2] == (ExecutionError, o.FK_DTYPE_CODE)
    assert o.raised(lambda: o.run_ooc(shape))[:2] == (ExecutionError, o.FK_DTYPE_CODE)
    # Control: an exactly representable orphan integer is admitted and typed double by both.
    oracle, run = _agree(d.float_parent_int_child_shape(policy, orphan_int=7))
    assert _signatures(oracle, run, "C") == ("k:string,n:double", "k:string,n:double")


@pytest.mark.parametrize("policy", [P, R])
def test_o10_date32_component(policy: OrphanPolicy) -> None:
    oracle, run = _agree(o.date_component_shape(policy))
    assert _signatures(oracle, run, "C") == ("k:string,n:date32[day]", "k:string,n:date32[day]")


# O11: the composite_key golden fixture.


@pytest.mark.parametrize("policy", POLICIES)
def test_o11_golden_composite_key_with_an_injected_orphan(policy: OrphanPolicy) -> None:
    _agree(d.golden_composite_shape(policy, orphan=True))


def test_o11_golden_fail_agrees_without_orphans_and_both_routes_raise_with_one() -> None:
    _agree(d.golden_composite_shape(F))
    shape = d.golden_composite_shape(F, orphan=True)
    oracle = o.raised(lambda: s.run_full(shape))
    assert oracle[:2] == (ExecutionError, o.ORPHAN_CODE)
    assert o.raised(lambda: o.run_ooc(shape)) == oracle


# O12: empty and all-null children (resident path).


@pytest.mark.parametrize("child", ["empty", "all-null"])
@pytest.mark.parametrize("policy", POLICIES)
def test_o12_empty_and_all_null_children_are_null_typed_on_both_routes(
    policy: OrphanPolicy, child: str
) -> None:
    rows = ([], []) if child == "empty" else ([None, None], [None, None])
    shape = d.composite_tables(d.key_table(["a", "b"], ["x", "y"]), d.key_table(*rows), policy)
    oracle, run = _agree(shape)
    assert _signatures(oracle, run, "C") == ("k:null,j:null", "k:null,j:null")
    assert run.warnings == ()


# O14: the caller-visible compute evidence.

_PARENT_NATIVE = [
    {"table": "P", "column": "k", "path": "mask_batch", "executed_backend": "rust_companion"},
    {"table": "P", "column": "j", "path": "mask_batch", "executed_backend": "rust_companion"},
]
_REMAP_PYTHON = [
    {
        "table": "C",
        "column": c,
        "path": "fk_remap",
        "executed_backend": "python_scalar",
        "reason": "fk_key_hash_remap_native_deferred:batch_join",
    }
    for c in ("k", "j")
]


def test_o14_hash_evidence_names_the_native_parent_keys_and_the_python_remap_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_companion(monkeypatch)
    assert o.hash_evidence(o.run_ooc(s.composite_shape(R))) == _PARENT_NATIVE + _REMAP_PYTHON
    assert o.hash_evidence(o.run_ooc(s.composite_shape(P))) == _PARENT_NATIVE
    # The oracle carries no out-of-core disposition: evidence is route evidence, not data.
    assert "out_of_core_hash" not in s.run_full(s.composite_shape(R)).quality_metrics


def test_o14_hash_evidence_reports_python_when_the_companion_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    force_companion_unavailable(monkeypatch)
    assert o.hash_evidence(o.run_ooc(s.composite_shape(P))) == [
        {
            "table": "P",
            "column": c,
            "path": "mask_batch",
            "executed_backend": "python_scalar",
            "reason": "crypto_extension_unavailable",
        }
        for c in ("k", "j")
    ]


# Section 5: pre-existing strict gaps, pinned with a scalar control each.

_NAN_PARENT_DIMS = {
    **o.METADATA_ONLY,
    "row_set": False,
    "row_order": False,
    "cellwise_values": False,
    "null_vs_nan": False,
}


@pytest.mark.parametrize("policy", [P, R])
def test_d0_g2_nan_in_a_parent_key_component_is_null_on_the_oracle_and_nan_out_of_core(
    policy: OrphanPolicy,
) -> None:
    shape = o.nan_parent_component_shape(policy)
    oracle, run = s.run_full(shape), o.run_ooc(shape)
    report = o.strict(oracle, run)
    assert report == {"P": _NAN_PARENT_DIMS, "C": o.METADATA_ONLY}
    assert oracle.outputs["P"].column("n").to_pylist() == [1.0, None]
    got = run.outputs["P"].column("n").to_pylist()
    assert got[0] == 1.0 and got[1] != got[1]  # NaN


_WIDTH_DIMS = {**o.METADATA_ONLY, "schema_structure": False, "type_width": False}


@pytest.mark.parametrize("policy", POLICIES)
def test_g5_zero_row_parent_is_typed_double_by_the_oracle_and_string_out_of_core(
    policy: OrphanPolicy,
) -> None:
    oracle, run = s.run_full(o.empty_parent_shape(policy)), o.run_ooc(o.empty_parent_shape(policy))
    assert o.strict(oracle, run) == {"P": _WIDTH_DIMS, "C": o.METADATA_ONLY}
    assert _signatures(oracle, run, "P") == ("k:double,j:double", "k:string,j:string")
    assert oracle.outputs["P"].num_rows == run.outputs["P"].num_rows == 0
    # Scalar control: a zero-row single-column parent shows the same oracle typing.
    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="hash",
        policy=policy,
        parent_rows=0,
        child_refs=[-1, -1],
        parent_nan_row=None,
    )
    control_oracle, control_ooc = _run_oracle(plan, sources, graph), _run_ooc(plan, sources, graph)
    assert sc.schema_signature(control_oracle.outputs["parent"]) == "pk:double,payload:null"
    assert sc.schema_signature(control_ooc.outputs["parent"]) == "pk:string,payload:string"


_TS_CHILD_DIMS = {
    **_WIDTH_DIMS,
    "row_set": False,
    "row_order": False,
    "cellwise_values": False,
}


@pytest.mark.parametrize("policy", [P, R])
def test_g6_timestamp_child_component_is_widened_to_nanoseconds_by_the_oracle_only(
    policy: OrphanPolicy,
) -> None:
    shape = o.timestamp_component_shape(policy)
    oracle, run = s.run_full(shape), o.run_ooc(shape)
    assert o.strict(oracle, run) == {"P": o.METADATA_ONLY, "C": _TS_CHILD_DIMS}
    assert _signatures(oracle, run, "C") == ("k:string,n:timestamp[ns]", "k:string,n:timestamp[us]")
    # Same instants: the difference is the unit the oracle's pandas write-back picked.
    assert (
        run.outputs["C"]
        .column("n")
        .cast(pa.timestamp("ns"))
        .equals(oracle.outputs["C"].column("n"))
    )
    control = o.scalar_control_shape(
        policy, pa.timestamp("us"), parent_n=[o.T1, o.T2], child_n=[o.T1, o.T3]
    )
    co, cr = s.run_full(control), o.run_ooc(control)
    assert _signatures(co, cr, "C") == ("n:timestamp[ns]", "n:timestamp[us]")


@pytest.mark.parametrize("policy", [P, R])
def test_g7_large_string_parent_component_keeps_its_width_out_of_core_only(
    policy: OrphanPolicy,
) -> None:
    shape = o.large_string_component_shape(policy)
    oracle, run = s.run_full(shape), o.run_ooc(shape)
    assert o.strict(oracle, run) == {"P": _WIDTH_DIMS, "C": o.METADATA_ONLY}
    assert _signatures(oracle, run, "P") == ("k:string,n:string", "k:string,n:large_string")
    control = o.scalar_control_shape(
        policy, pa.large_string(), parent_n=["x", "y"], child_n=["x", "w"]
    )
    co, cr = s.run_full(control), o.run_ooc(control)
    assert _signatures(co, cr, "P") == ("n:string", "n:large_string")
    assert _signatures(co, cr, "C") == ("n:string", "n:string")


_GOLDEN_ORPHAN = ("orphan_fk", "member_id,plan_id,effective_date")
_GOLDEN_CLAIMS = ("undeclared_output_columns", "claims")
_GOLDEN_ENROLLMENTS = ("undeclared_output_columns", "enrollments")


def _order(warnings: Any) -> list[tuple[str, str]]:
    return [(w.code, w.detail.get("table") or w.column) for w in warnings]


def test_g9_warning_order_across_tables_is_route_dependent_while_the_warning_set_is_not() -> None:
    """Pre-existing on every route pair (a single-edge scalar job shows it too): the oracle
    emits node warnings in dispatch order and projection warnings last, in source order; the
    sequential route emits per table in topological order with each table's projection
    warning first; the out-of-core route emits per table in topological order with the
    orphan aggregate after the stream. The objects are identical, so a consumer that keys on
    the warning is unaffected; one that keys on position is not."""
    shape = d.golden_composite_shape(W, orphan=True)
    oracle, seq, run = s.run_full(shape), s.run_seq(shape), o.run_ooc(shape)
    assert _order(oracle.warnings) == [_GOLDEN_ORPHAN, _GOLDEN_CLAIMS, _GOLDEN_ENROLLMENTS]
    assert _order(seq.warnings) == [_GOLDEN_ENROLLMENTS, _GOLDEN_ORPHAN, _GOLDEN_CLAIMS]
    assert _order(run.warnings) == [_GOLDEN_ENROLLMENTS, _GOLDEN_CLAIMS, _GOLDEN_ORPHAN]
    assert o.sorted_warnings(run.warnings) == o.sorted_warnings(oracle.warnings)
    assert o.sorted_warnings(seq.warnings) == o.sorted_warnings(oracle.warnings)
    # Scalar control: the single-edge chain with unconfigured payload columns orders the same way.
    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="hash",
        policy=W,
        parent_rows=3,
        child_refs=[0, -1],
        parent_nan_row=None,
    )
    assert _order(_run_oracle(plan, sources, graph).warnings) == [
        ("orphan_fk", "fk"),
        ("undeclared_output_columns", "parent"),
        ("undeclared_output_columns", "child"),
    ]
    assert _order(_run_ooc(plan, sources, graph).warnings) == [
        ("undeclared_output_columns", "parent"),
        ("undeclared_output_columns", "child"),
        ("orphan_fk", "fk"),
    ]


@pytest.mark.parametrize("policy", [P, R])
def test_g8_bool_component_fails_closed_out_of_core(policy: OrphanPolicy) -> None:
    shape = o.bool_component_shape(policy)
    assert o.raised(lambda: o.run_ooc(shape))[:2] == (ExecutionError, o.FK_DTYPE_CODE)


@pytest.mark.xfail(
    strict=True,
    raises=pa.ArrowInvalid,
    reason="G8: the oracle crashes uncoded on a preserved orphan bool component (fk_key_value's "
    "bool-to-int fold); an owner decision item. Fixing the oracle turns this XPASS and the "
    "test is then deleted with the SEMANTIC_DIFFERENCES row.",
)
@pytest.mark.parametrize("policy", [P, R])
def test_g8_the_oracle_raises_a_coded_error_on_a_bool_component(policy: OrphanPolicy) -> None:
    with pytest.raises(ExecutionError):
        s.run_full(o.bool_component_shape(policy))


# Section 6: declines, coded and caller-visible.


def _scalar_child_shape(policy: OrphanPolicy) -> s.Shape:
    plan = s.make_plan(
        {
            "P": s.table_seed({"k": s.hashed("ns_pk"), "j": s.hashed("ns_pj")}),
            "C": s.table_seed({"k": s.hashed("ns_ck"), "j": s.hashed("ns_cj")}),
        }
    )
    base = s.composite_shape(policy)
    return s.Shape(plan, base.graph, base.sources)


def _route(shape: s.Shape) -> tuple[str, str]:
    compatible, code = out_of_core_admission(shape.plan, registry=s.REGISTRY, graph=shape.graph)
    return decide_execution_route(
        SimpleNamespace(relationships=(object(),), tables=()),
        has_generate_table=False,
        has_mask_table=True,
        validators=[],
        fidelity_report=False,
        vault_writer=None,
        execution_mode="auto",
        graph=shape.graph,
        out_of_core_compatible=compatible,
        out_of_core_reject_code=code,
        largest_table_rows=10,
    )


def test_d1_scalar_child_double_masking_stays_declined_at_the_gate_and_in_the_runner() -> None:
    shape = _scalar_child_shape(P)
    assert _gate_codes(shape) == [SCALAR_CHILD_CODE, SCALAR_CHILD_CODE]
    assert o.raised(lambda: o.run_ooc(shape))[:2] == (ExecutionError, SCALAR_CHILD_CODE)
    assert _route(shape) == ("sequential", "pure_mask_fk")


@pytest.mark.parametrize(
    "values", [(1, 2, 7), (d.UBIG, 2, d.UBIG + 1)], ids=["small", "beyond-int64"]
)
@pytest.mark.parametrize("policy", POLICIES)
def test_d2_uint64_component_is_the_sanctioned_oracle_success_ooc_reject(
    policy: OrphanPolicy, values: tuple[int, int, int]
) -> None:
    shape = o.uint64_component_shape(policy, values=values)
    assert s.run_full(shape).outputs["C"].column("n").to_pylist() == [values[0], values[2]]
    assert o.raised(lambda: o.run_ooc(shape))[:2] == (ExecutionError, o.FK_DTYPE_CODE)
    assert _gate_codes(shape) == []  # the static gate cannot see dtypes; the joiner rejects


def test_d2_decimal_component_is_the_sanctioned_oracle_success_ooc_reject() -> None:
    shape = s.mixed_composite_shape(R)
    assert s.run_full(shape).outputs["C"].num_rows == 3
    assert o.raised(lambda: o.run_ooc(shape))[:2] == (ExecutionError, o.FK_DTYPE_CODE)


@pytest.mark.parametrize("policy", [P, R, F])
def test_d3_composite_chain_declines_with_its_own_code_and_routes_to_sequential(
    policy: OrphanPolicy,
) -> None:
    shape = s.composite_chain_shape(policy)
    assert _gate_codes(shape) == [CHAIN_CODE]  # once per edge, not once per key column
    kind, code, message = o.raised(lambda: o.run_ooc(shape))
    assert (kind, code) == (ExecutionError, CHAIN_CODE)
    assert "B.('k', 'j')" in message
    for cell in ("a", "b", "x", "y"):
        assert f"'{cell}'" not in message
    assert _route(shape) == ("sequential", "pure_mask_fk")


def test_d3_a_parent_column_absent_from_the_plan_keeps_the_seed_missing_code() -> None:
    plan = s.make_plan(
        {"P": s.table_seed({"k": s.hashed("ns_pk")}), "C": s.table_seed({}, {("k", "j"): "ns_cg"})}
    )
    g = s.graph(s.edge("P", ("k", "j"), "C", ("k", "j"), "ns_cg", P))
    shape = s.Shape(plan, g, s.composite_shape(P).sources)
    assert _gate_codes(shape) == ["out_of_core_parent_seed_missing"]
