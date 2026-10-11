"""Phase D / D0: frozen strict-parity baseline for the OOC FK chain (no kernel).

The normalized harness (`test_out_of_core_fk_parity.py`) folds NaN to null,
ignores Arrow width and lets `1 == 1.0`. This module pins where the OOC route
(`run_fk_out_of_core`) and the pandas oracle agree and disagree at the STRICT
level on the parent -> child single edge, so a later FK-kernel slice cannot
widen the divergence unnoticed. It characterizes reality; it does NOT claim
strict parity holds, and it fixes none of the gaps (see
`SEMANTIC_DIFFERENCES.md`, "Phase D / D0 strict baseline").

Every expected schema, metadata key set, cell list, per-dimension verdict and
error code below is a hardcoded literal asserted against the actual run. None is
derived from the output under test. The literals were probed against the engine
at origin/main 49b88677 before being pinned.

The witness half pins the negative baseline: the chain is served entirely by the
legacy kernels, so `assert_legacy_path_only` holds and `assert_native_ran` fails.
A kernel slice flips exactly that assertion for the cells it claims.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from tests.parity import _strict_compare as sc
from tests.parity._kernel_witness import ExpectedMask, KernelWitness
from tests.parity._strict_compare import _cell_token
from tests.parity.test_out_of_core_fk_parity import (
    OrphanPolicy,
    _build_single_edge,
    _gate_admits,
    _run_ooc,
    _run_oracle,
)

# derive(job_seed=b"\x11"*8, "ns_p", canonical(i)).hex() for i in 0, 1, 2.
_H = (
    "9bcd47a35e88865673ce2eca131420544e90f20cef24ada1ee8c45bcb2a4fbcf",
    "7447203bd08282ca180a2b57739f53a08845219aba087bfcd5a96f2ba0a911c5",
    "4d6bbbb083042f6be7487c108d134a4c23aa5577467d423e999e88896caa96b0",
)
_NAN = float("nan")
_PP = ["pp0", "pp1", "pp2"]
_CP = ["cp0", "cp1", "cp2"]
# The oracle round-trips through pandas and keeps its schema metadata; the OOC
# path preserves the (empty) source metadata. Same on every success fixture.
_ORACLE_META = [b"pandas"]
_OOC_META: list[bytes] = []


@dataclass(frozen=True)
class TableExpect:
    """Pinned strict outcome for one output table."""

    oracle_schema: str
    ooc_schema: str
    oracle_cells: Mapping[str, Sequence[object]]
    ooc_cells: Mapping[str, Sequence[object]]
    # Dimensions on which the two executors disagree; every other one agrees.
    disagree: frozenset[str] = frozenset({"field_metadata"})


@dataclass(frozen=True)
class Case:
    id: str
    parent_kind: str
    child_kind: str
    strategy: str
    child_refs: list[int | None]
    nan_row: int | None
    parent: TableExpect
    child: TableExpect
    policy: OrphanPolicy = OrphanPolicy.FAIL
    parent_rows: int = 3
    masked: list[ExpectedMask] = field(default_factory=list)

    def build(self) -> tuple[Any, dict[str, pa.Table], Any]:
        return _build_single_edge(
            parent_kind=self.parent_kind,
            child_kind=self.child_kind,
            strategy=self.strategy,
            policy=self.policy,
            parent_rows=self.parent_rows,
            child_refs=self.child_refs,
            parent_nan_row=self.nan_row,
        )


def _same(schema: str, cells: Mapping[str, Sequence[object]]) -> TableExpect:
    return TableExpect(schema, schema, cells, cells)


def _ok_case(strategy: str, keys: list[object], key_type: str) -> Case:
    pk = f"pk:{key_type},payload:string"
    fk = f"fk:{key_type},payload:string"
    return Case(
        id=f"D0-ok-{strategy}",
        parent_kind="int",
        child_kind="int",
        strategy=strategy,
        child_refs=[0, 1, 2],
        nan_row=None,
        parent=_same(pk, {"pk": keys, "payload": _PP}),
        child=_same(fk, {"fk": keys, "payload": _CP}),
        masked=[ExpectedMask("parent", "pk", f"{strategy}_array")],
    )


_SUCCESS_CASES = [
    _ok_case("hash", list(_H), "string"),
    _ok_case("redact", ["REDACTED"] * 3, "string"),
    _ok_case("truncate", ["0", "1", "2"], "string"),
    _ok_case("passthrough", [0, 1, 2], "int64"),
    Case(
        id="D0-nan-preserve",
        parent_kind="float",
        child_kind="float",
        strategy="passthrough",
        child_refs=[None, 1, 2],
        nan_row=0,
        parent=TableExpect(
            "pk:double,payload:string",
            "pk:double,payload:string",
            {"pk": [None, 1.0, 2.0], "payload": _PP},
            {"pk": [_NAN, 1.0, 2.0], "payload": _PP},
            frozenset({"field_metadata", "row_set", "row_order", "cellwise_values", "null_vs_nan"}),
        ),
        child=_same("fk:double,payload:string", {"fk": [None, 1.0, 2.0], "payload": _CP}),
        masked=[ExpectedMask("parent", "pk", "passthrough_array")],
    ),
    Case(
        id="D0-width-agree",
        parent_kind="int",
        child_kind="float",
        strategy="passthrough",
        child_refs=[0, 1, 2],
        nan_row=None,
        parent=_same("pk:int64,payload:string", {"pk": [0, 1, 2], "payload": _PP}),
        child=_same("fk:int64,payload:string", {"fk": [0, 1, 2], "payload": _CP}),
        masked=[ExpectedMask("parent", "pk", "passthrough_array")],
    ),
    Case(
        id="D0-empty",
        parent_kind="int",
        child_kind="int",
        strategy="hash",
        child_refs=[],
        nan_row=None,
        parent=_same("pk:string,payload:string", {"pk": list(_H), "payload": _PP}),
        child=TableExpect(
            "fk:int64,payload:null",
            "fk:null,payload:string",
            {"fk": [], "payload": []},
            {"fk": [], "payload": []},
            frozenset({"field_metadata", "schema_structure", "type_width"}),
        ),
        # The 0-row child masks nothing, but the 3 parent rows are still masked.
        masked=[ExpectedMask("parent", "pk", "hash_array")],
    ),
    Case(
        id="D0-allnull",
        parent_kind="int",
        child_kind="int",
        strategy="hash",
        child_refs=[None, None, None],
        nan_row=None,
        parent=_same("pk:string,payload:string", {"pk": list(_H), "payload": _PP}),
        child=TableExpect(
            "fk:int64,payload:string",
            "fk:null,payload:string",
            {"fk": [None, None, None], "payload": _CP},
            {"fk": [None, None, None], "payload": _CP},
            frozenset({"field_metadata", "schema_structure", "type_width"}),
        ),
        masked=[ExpectedMask("parent", "pk", "hash_array")],
    ),
]
_IDS = [c.id for c in _SUCCESS_CASES]


def _tokens(values: Sequence[object]) -> list[tuple[object, ...]]:
    return [_cell_token(v) for v in values]


def _assert_table(
    actual: pa.Table, schema: str, meta: list[bytes], cells: Mapping[str, Sequence[object]]
) -> None:
    assert sc.schema_signature(actual) == schema
    assert sc.metadata_keys(actual) == meta
    assert actual.column_names == list(cells)
    for name, want in cells.items():
        assert _tokens(actual.column(name).to_pylist()) == _tokens(want), name


@pytest.mark.parametrize("case", _SUCCESS_CASES, ids=_IDS)
def test_success_case_strict_baseline(case: Case) -> None:
    plan, sources, graph = case.build()
    # Admitted AND successful on both routes: a formerly-successful case may
    # never silently become a rejection.
    assert _gate_admits(plan, graph) is True
    oracle = _run_oracle(plan, sources, graph).outputs
    ooc = _run_ooc(plan, sources, graph).outputs
    for table, want in (("parent", case.parent), ("child", case.child)):
        _assert_table(oracle[table], want.oracle_schema, _ORACLE_META, want.oracle_cells)
        _assert_table(ooc[table], want.ooc_schema, _OOC_META, want.ooc_cells)
        report = sc.compare_strict(oracle[table], ooc[table])
        assert report == {dim: dim not in want.disagree for dim in sc.DIMENSIONS}, table


@pytest.mark.parametrize("case", _SUCCESS_CASES, ids=_IDS)
def test_success_case_native_witness_negative_baseline(case: Case) -> None:
    plan, sources, graph = case.build()
    witness = KernelWitness()
    # Spies are scoped to the OOC call only; the oracle runs outside the block.
    with witness.observe():
        ooc = _run_ooc(plan, sources, graph).outputs
    # Every expected-masked column was observed (the empty child masks nothing,
    # yet the parent's 3 rows are still masked, so its entrypoint IS observed).
    witness.assert_covered(sources, case.masked)
    witness.assert_legacy_path_only(sources, case.masked)
    with pytest.raises(AssertionError):
        witness.assert_native_ran(sources, ooc, case.masked)


def test_orphan_fail_both_raise_same_code() -> None:
    plan, sources, graph = _build_single_edge(
        parent_kind="int",
        child_kind="int",
        strategy="hash",
        policy=OrphanPolicy.FAIL,
        parent_rows=3,
        child_refs=[0, -1, 1],
        parent_nan_row=None,
    )
    assert _gate_admits(plan, graph) is True
    with pytest.raises(ExecutionError) as oracle_exc:
        _run_oracle(plan, sources, graph)
    with pytest.raises(ExecutionError) as ooc_exc:
        _run_ooc(plan, sources, graph)
    assert type(oracle_exc.value) is type(ooc_exc.value) is ExecutionError
    assert oracle_exc.value.code == "orphan_fk_violation"
    assert ooc_exc.value.code == "orphan_fk_violation"


def test_dtype_reject_is_the_single_sanctioned_ooc_rejection() -> None:
    plan, sources, graph = _build_single_edge(
        parent_kind="int",
        child_kind="decimal",
        strategy="hash",
        policy=OrphanPolicy.PRESERVE,
        parent_rows=3,
        child_refs=[0, 1, 2],
        parent_nan_row=None,
    )
    assert _gate_admits(plan, graph) is True
    oracle = _run_oracle(plan, sources, graph).outputs
    assert sc.schema_signature(oracle["child"]) == "fk:string,payload:string"
    assert oracle["child"].column("fk").to_pylist() == list(_H)
    # Reject-rather-than-drift: a typed ExecutionError with the exact code. A
    # failed assertion is an AssertionError and would not satisfy this raises().
    with pytest.raises(ExecutionError) as ooc_exc:
        _run_ooc(plan, sources, graph)
    assert type(ooc_exc.value) is ExecutionError
    assert ooc_exc.value.code == "out_of_core_fk_key_dtype_unsupported"
