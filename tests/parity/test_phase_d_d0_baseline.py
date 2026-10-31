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

The `_H` hash literals were re-pinned under F13 because job_seed keying was removed; they are now
keyed on the shared throwaway secret.

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
from tests.parity._kernel_witness import ExpectedMask, KernelWitness, force_companion_unavailable
from tests.parity._strict_compare import _cell_token
from tests.parity.test_out_of_core_fk_parity import (
    OrphanPolicy,
    _build_single_edge,
    _gate_admits,
    _run_ooc,
    _run_oracle,
)

# hash_array(int64 [0, 1, 2], THROWAWAY_KEY_PROVIDER.mask_key(), "ns_p").
_H = (
    "615fa05252975052a3734a0d5a6d5e880ba3b8fd29ee4b77d368f04b30398b94",
    "08a13b4caf4e739f4be2c955d1360f5c65c07b97432fd73eafb0d25b263ad86f",
    "d3da9ec9554ff1e2c94fef1b22d7a3c5048df6a6d9ec5147487c36ffbcad9f1a",
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


# Every field on every fixture, both routes, probed at 49b88677: nullable, no
# field-level metadata. Pinned exactly so a change inside a "known gap"
# dimension (field_metadata / schema_structure) is still caught.
_FIELD_NULLABLE = True
_FIELD_METADATA = None


def _expected_fields(schema: str) -> list[tuple[str, str, bool, dict[bytes, bytes] | None]]:
    """`name:type,...` literal -> exact (name, type, nullable, metadata) pins."""
    pairs = (part.split(":") for part in schema.split(","))
    return [(n, t, _FIELD_NULLABLE, _FIELD_METADATA) for n, t in pairs]


def _assert_table(
    actual: pa.Table, schema: str, meta: list[bytes], cells: Mapping[str, Sequence[object]]
) -> None:
    """Exact per-executor pin: field name/type/nullability/metadata, the exact
    schema-level key set, and cells.

    The VALUE of the schema-level `pandas` key varies across pandas versions and
    is the one permitted variation: only its presence is pinned. Any other
    schema-level key, and any field-level metadata, fails.
    """
    got = [(f.name, str(f.type), f.nullable, f.metadata) for f in actual.schema]
    assert got == _expected_fields(schema)
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
def test_success_case_native_witness_negative_baseline(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D1a routes admitted hash columns native when the companion is present; this control
    # pins the legacy path, so it hides the companion rather than depend on the box.
    force_companion_unavailable(monkeypatch)
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


# --- Negative controls: the baseline pin bites on a mutated copy of real output.
# The real route is never touched; each mutation is applied to an in-memory copy.


def _real_ooc_parent() -> pa.Table:
    case = _SUCCESS_CASES[0]  # D0-ok-hash
    plan, sources, graph = case.build()
    return _run_ooc(plan, sources, graph).outputs["parent"]


def _pin_ooc_parent(table: pa.Table) -> None:
    want = _SUCCESS_CASES[0].parent
    _assert_table(table, want.ooc_schema, _OOC_META, want.ooc_cells)


def _with_schema(table: pa.Table, schema: pa.Schema) -> pa.Table:
    return pa.Table.from_arrays(table.columns, schema=schema)


def test_baseline_pin_accepts_the_real_output() -> None:
    _pin_ooc_parent(_real_ooc_parent())


def test_baseline_pin_fails_on_added_field_metadata() -> None:
    real = _real_ooc_parent()
    field = real.schema.field("pk").with_metadata({b"unit": b"cm"})
    mutated = _with_schema(real, real.schema.set(0, field))
    with pytest.raises(AssertionError):
        _pin_ooc_parent(mutated)


def test_baseline_pin_fails_on_flipped_nullability() -> None:
    real = _real_ooc_parent()
    field = real.schema.field("payload").with_nullable(False)
    mutated = _with_schema(real, real.schema.set(1, field))
    with pytest.raises(AssertionError):
        _pin_ooc_parent(mutated)


def test_baseline_pin_fails_on_unexpected_schema_metadata_key() -> None:
    mutated = _real_ooc_parent().replace_schema_metadata({b"unexpected": b"x"})
    with pytest.raises(AssertionError):
        _pin_ooc_parent(mutated)


def test_baseline_pin_fails_on_pandas_key_added_to_ooc() -> None:
    mutated = _real_ooc_parent().replace_schema_metadata({b"pandas": b"{}"})
    with pytest.raises(AssertionError):
        _pin_ooc_parent(mutated)


def test_baseline_pin_tolerates_a_changed_pandas_key_value_on_the_oracle() -> None:
    # The pandas key's bytes differ across pandas versions: intentionally not pinned.
    case = _SUCCESS_CASES[0]
    plan, sources, graph = case.build()
    real = _run_oracle(plan, sources, graph).outputs["parent"]
    mutated = real.replace_schema_metadata({b"pandas": b'{"other": "version"}'})
    _assert_table(mutated, case.parent.oracle_schema, _ORACLE_META, case.parent.oracle_cells)
    # ...but a second schema-level key alongside it is not.
    extra = real.replace_schema_metadata({b"pandas": b"{}", b"extra": b"1"})
    with pytest.raises(AssertionError):
        _assert_table(extra, case.parent.oracle_schema, _ORACLE_META, case.parent.oracle_cells)
