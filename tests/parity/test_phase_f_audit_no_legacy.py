"""Phase F / F-audit: the no-legacy GENERATION compute audit (consolidated sweep).

Generation has no compiled path by design (FR3 shelved), so the question is not "did a kernel
run" but: does every generator run on the compute backend its dispatch predicates classify it
for, on the core when admitted and on the legacy whole-table generator only when a predicate says
so, with the same values on both materialization routes.

The matrix (`_f_audit_matrix`) is derived from the predicates. This sweep compares it with the
real `classify_plan`, then proves each cell with consumed-output witnesses:

- core cells: a FAIL-ON-ENTRY guard on `_run_legacy` plus an actual-core assertion (the witness
  instruments BOTH paths, so a Python or numpy label alone does not prove the core), both
  materialization routes, and a value-equality witness on a nonempty live set. `sequence`,
  `reference` (sequential) and `windowed_date` emit no selection event, so their producer output
  is MARKED at the real boundary (`_f_audit_witness`) and must be what the column holds;
- legacy cells: `classify_plan(...).eligible is False` with the expected reason and a
  record-and-delegate spy proves `_run_legacy` ran; kinds the witness can observe are also
  value-checked, the rest are proven by route and reason alone;
- every witnessed cell has a named discard-and-SUBSTITUTE mutant that must fail the witness.

Evidence reuse (no duplication): route independence over irregular partitions is already proven
for the rows in `REUSE` below by `test_phase_f_fr0_route_independence.py` and
`tests/unit/generation/test_fr1_windowed_date.py`. This file adds what they lack: the core-entry
guard, the declared-schema check on the public entries, the marked producer boundary, and the
mutants.
"""

from __future__ import annotations

import dataclasses
import importlib
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.generation import _column_streams, _plan_entry, _streamed
from decoy_engine.generation._admission import PlanAdmission, classify_plan
from decoy_engine.generation._streamed import GenerationResult
from tests.parity import _f0_witness as w
from tests.parity import _f_audit_cells as cells
from tests.parity import _f_audit_matrix as mx
from tests.parity._f_audit_support import BoundedSink, forbid_legacy, prepare, run_cell
from tests.parity._f_audit_witness import audit
from tests.parity._fr0_support import fingerprints, plan_of, prepared_config

UNMARKED_WINDOWED = _column_streams.WindowedDateStream.next_batch_from
CORE_CELLS = cells.core_cells(UNMARKED_WINDOWED)
LEGACY_CELLS = cells.legacy_cells(UNMARKED_WINDOWED)
# Rows proven only by the vacuity fence (`test_phase_f_audit_vacuity.py`).
FENCE_ONLY = {"reference-empty-parent"}

# Matrix row -> an existing test that already proves resident == streamed over irregular
# partitions for it. The sweep does not repeat those value comparisons.
_FR0 = "tests.parity.test_phase_f_fr0_route_independence"
_STREAMED_EQ = "test_streamed_route_equals_the_resident_route"
_ANY_PARTITION = "test_any_partition_gives_the_same_bytes_on_both_routes"
REUSE = {
    "sequence": (_FR0, _STREAMED_EQ),
    "categorical": (_FR0, _STREAMED_EQ),
    "faker-below-threshold": (_FR0, _STREAMED_EQ),
    "reference-sequential": (_FR0, _ANY_PARTITION),
    "reference-random": (_FR0, _ANY_PARTITION),
    "reference-weighted": (_FR0, _ANY_PARTITION),
    "faker-pooled": (
        _FR0,
        "test_pooled_oracle_is_reproduced_across_the_pool_threshold_in_every_layout",
    ),
    "windowed-date": (
        "tests.unit.generation.test_fr1_windowed_date",
        "test_resident_streamed_and_legacy_agree",
    ),
}


def _cell_id(cell: cells.Cell) -> str:
    return cell.row_id


# --------------------------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------------------------


def _admission(cell: cells.Cell) -> PlanAdmission:
    return classify_plan(prepared_config(cell.build()), provider_snapshot=cell.provider_snapshot)


def _column(adm: PlanAdmission, key: tuple[str, str]) -> Any:
    return next(c for c in adm.columns if (c.table, c.column) == key)


# --------------------------------------------------------------------------------------------
# coverage and the matrix-versus-predicate cross-check
# --------------------------------------------------------------------------------------------


def test_every_matrix_row_has_a_cell() -> None:
    covered = {c.row_id for c in (*CORE_CELLS, *LEGACY_CELLS)} | FENCE_ONLY
    assert covered == {r.row_id for r in mx.ROWS}


@pytest.mark.parametrize("cell", [*CORE_CELLS, *LEGACY_CELLS], ids=_cell_id)
def test_matrix_admission_reason_and_route_match_the_real_classifier(cell: cells.Cell) -> None:
    row = mx.row(cell.row_id)
    adm = _admission(cell)
    key = cell.admission_key or next(iter(cell.columns))
    col = _column(adm, key)
    assert col.eligible is row.admission
    assert (
        col.reason.startswith(row.reason) if row.reason != "eligible" else col.reason == "eligible"
    )
    assert adm.eligible is (row.route == mx.CORE)


@pytest.mark.parametrize("kind", sorted(mx.ADMITTED_KINDS | {"formula", "statistical", "derived"}))
def test_determinism_fresh_declines_every_kind_before_the_kind_dispatch(kind: str) -> None:
    config = {
        "tables": [
            {"name": "t", "generate_columns": [{"name": "c", "type": kind, "determinism": "fresh"}]}
        ]
    }
    col = classify_plan(config).columns[0]
    assert (col.eligible, col.reason) == (False, "determinism_fresh")


_ADMITTED_COLUMNS: dict[str, dict[str, Any]] = {
    "sequence": {},
    "categorical": {"categories": ["a", "b"]},
    "faker": {"faker_type": "city"},
    "reference": {"reference_table": "par", "reference_column": "k"},
    "windowed_date": {"anchor": "anchor"},
}


@pytest.mark.parametrize("kind", sorted(_ADMITTED_COLUMNS))
def test_one_declined_column_sends_an_admitted_sibling_and_other_tables_to_legacy(
    kind: str,
) -> None:
    anchor = {"name": "anchor", "type": "categorical", "categories": ["2024-03-01"]}
    column = {"name": "c", "type": kind, **_ADMITTED_COLUMNS[kind]}
    config = {
        "tables": [
            {"name": "lone", "generate_columns": [{"name": "fm", "type": "formula"}]},
            {"name": "par", "generate_columns": [{"name": "k", "type": "categorical", "categories": ["K"]}]},
            {"name": "other", "generate_columns": [anchor, column]},
        ]
    }  # fmt: skip
    adm = classify_plan(config)
    mine = _column(adm, ("other", "c"))
    assert (mine.eligible, mine.reason) == (True, "eligible")
    assert adm.eligible is False
    assert adm.reason() == "lone.fm:family_declined:formula"


def test_reuse_mapping_names_real_tests() -> None:
    assert set(REUSE) <= {c.row_id for c in CORE_CELLS}
    for module, name in set(REUSE.values()):
        assert hasattr(importlib.import_module(module), name), (module, name)


# --------------------------------------------------------------------------------------------
# core cells: fail-on-entry guard, actual core, both routes, consumed-output witness
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("route", [mx.RESIDENT, mx.STREAMED])
@pytest.mark.parametrize("cell", CORE_CELLS, ids=_cell_id)
def test_core_cell_computes_on_its_classified_backend(
    cell: cells.Cell, route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(cell, monkeypatch)
    forbid_legacy(monkeypatch)
    with audit(monkeypatch, marking=True) as witness:
        result, tables = run_cell(cell, route, tmp_path)
    assert result.mode == route
    assert not result.reason.startswith("legacy:")
    assert set(cell.columns) <= witness.core_keys, "the columns were not built by the core"
    reports = witness.classify(tables)
    for key, owner in cell.columns.items():
        assert reports[key].selection == {mx.row(owner).compute}, key
        assert reports[key].evidence == w.REAL


@pytest.mark.parametrize("cell", CORE_CELLS, ids=_cell_id)
def test_public_entries_take_the_core_and_keep_the_declared_schema(
    cell: cells.Cell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`generate_tables` (resident) and `generate_tables_streamed` (bounded sink, a different
    batch partition) agree with `run_generation` on values, and every table carries the schema
    `classify_plan` declared (never an inferred Arrow null)."""
    prepare(cell, monkeypatch)
    forbid_legacy(monkeypatch)
    plan = plan_of(cell.build())
    declared = classify_plan(prepared_config(cell.build()))
    resident = _plan_entry.generate_tables(plan)
    target = tmp_path / "out"
    streamed = _plan_entry.generate_tables_streamed(
        plan, sink=ParquetTransactionalSink(target), batch_rows=7
    )
    assert streamed.mode == "streamed" and streamed.tables is None
    committed = {p.stem: pq.read_table(p) for p in sorted(target.glob("*.parquet"))}
    _, partitioned = run_cell(cell, mx.RESIDENT, tmp_path)
    for name, table in resident.items():
        want = declared.schema(name)
        for got in (table, committed[name], partitioned[name]):
            assert got.schema.names == want.names
            assert [f.type for f in got.schema] == [f.type for f in want], name
    if cell.row_id not in REUSE:
        assert fingerprints(resident) == fingerprints(committed) == fingerprints(partitioned)


@pytest.mark.parametrize("cell", CORE_CELLS, ids=_cell_id)
def test_forced_legacy_routing_trips_the_core_entry_assertions(
    cell: cells.Cell, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutant: the admitted plan is pushed onto the legacy path. The fail-on-entry guard trips,
    and with the guard lifted the core-built assertion still trips even when values agree."""
    prepare(cell, monkeypatch)
    real = _streamed.classify_plan

    def forced(config: dict[str, Any], **kw: Any) -> PlanAdmission:
        adm = real(config, **kw)
        return PlanAdmission(
            tuple(dataclasses.replace(c, eligible=False, reason="forced") for c in adm.columns)
        )

    monkeypatch.setattr(_streamed, "classify_plan", forced)
    with monkeypatch.context() as guard:
        forbid_legacy(guard)
        with pytest.raises(AssertionError, match="entered the legacy"):
            run_cell(cell, mx.RESIDENT, tmp_path / "guarded")
    with audit(monkeypatch, marking=True) as witness:
        result, _ = run_cell(cell, mx.RESIDENT, tmp_path / "lifted")
    assert result.reason.startswith("legacy:")
    assert not (set(cell.columns) <= witness.core_keys)


# --------------------------------------------------------------------------------------------
# legacy cells: route and reason, and the value witness where one can be compared
# --------------------------------------------------------------------------------------------


def _run_legacy_cell(
    cell: cells.Cell, mp: pytest.MonkeyPatch
) -> tuple[GenerationResult, BoundedSink, list[int]]:
    entered: list[int] = []
    real = _streamed._run_legacy

    def spy(*a: Any, **kw: Any) -> GenerationResult:
        entered.append(1)
        return real(*a, **kw)

    mp.setattr(_streamed, "_run_legacy", spy)
    sink = BoundedSink()
    result = _plan_entry.generate_tables_streamed(
        plan_of(cell.build()), sink=sink, provider_snapshot=cell.provider_snapshot
    )
    return result, sink, entered


@pytest.mark.parametrize("cell", LEGACY_CELLS, ids=_cell_id)
def test_legacy_cell_takes_run_legacy_with_the_expected_reason(
    cell: cells.Cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(cell, monkeypatch)
    row = mx.row(cell.row_id)
    adm = _admission(cell)
    assert adm.eligible is False
    result, sink, entered = _run_legacy_cell(cell, monkeypatch)
    assert entered == [1], "run_generation did not take _run_legacy"
    assert (result.mode, sink.calls) == (mx.RESIDENT, []), "a legacy plan is resident only"
    assert result.reason.startswith("legacy:")
    key = cell.admission_key or next(iter(cell.columns))
    decline = f"{key[0]}.{key[1]}:{row.reason}"
    assert any(d.startswith(decline) for d in result.declines) or row.reason == "eligible"
    assert result.tables is not None


@pytest.mark.parametrize("cell", [c for c in LEGACY_CELLS if c.columns], ids=_cell_id)
def test_legacy_cell_value_witness(cell: cells.Cell, monkeypatch: pytest.MonkeyPatch) -> None:
    prepare(cell, monkeypatch)
    with audit(monkeypatch) as witness:
        result, _, entered = _run_legacy_cell(cell, monkeypatch)
    assert entered == [1] and result.tables is not None
    assert witness.core_keys == set(), "a legacy plan built a core stream"
    reports = witness.classify(result.tables)
    for key, owner in cell.columns.items():
        assert reports[key].selection == {mx.row(owner).compute}, key


@pytest.mark.parametrize("cell", [c for c in LEGACY_CELLS if not c.columns], ids=_cell_id)
def test_excluded_legacy_kinds_are_proven_by_route_and_reason_not_a_compute_witness(
    cell: cells.Cell,
) -> None:
    assert mx.row(cell.row_id).witness == mx.EXCLUDED
    assert mx.row(cell.row_id).witness_why


# --------------------------------------------------------------------------------------------
# mutants: execute the producer, discard its output, substitute a recomputed value
# --------------------------------------------------------------------------------------------

_CORE_MUTANTS = [(c, m) for c in CORE_CELLS for m in c.mutants]
_LEGACY_MUTANTS = [(c, m) for c in LEGACY_CELLS for m in c.mutants]


def _mutant_id(value: Any) -> str:
    return value.name if isinstance(value, cells.Mutant) else value.row_id


@pytest.mark.parametrize("route", [mx.RESIDENT, mx.STREAMED])
@pytest.mark.parametrize(("cell", "mutant"), _CORE_MUTANTS, ids=_mutant_id)
def test_core_substitution_mutant_fails_the_value_witness(
    cell: cells.Cell,
    mutant: cells.Mutant,
    route: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare(cell, monkeypatch)
    with audit(monkeypatch, marking=True) as witness:
        mutant.install(monkeypatch)
        _, tables = run_cell(cell, route, tmp_path)
    with pytest.raises(w.WitnessError, match=mutant.match):
        witness.classify(tables)


@pytest.mark.parametrize(("cell", "mutant"), _LEGACY_MUTANTS, ids=_mutant_id)
def test_legacy_substitution_mutant_fails_the_value_witness(
    cell: cells.Cell, mutant: cells.Mutant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(cell, monkeypatch)
    with audit(monkeypatch) as witness:
        mutant.install(monkeypatch)
        result, _, _ = _run_legacy_cell(cell, monkeypatch)
    assert result.tables is not None
    with pytest.raises(w.WitnessError, match=mutant.match):
        witness.classify(result.tables)


def test_every_witnessed_cell_has_a_named_mutant() -> None:
    for cell in (*CORE_CELLS, *LEGACY_CELLS):
        if cell.columns and mx.row(cell.row_id).witness == mx.VALUE_EQUALITY:
            assert cell.mutants, cell.row_id


def test_a_wrong_column_attribution_of_the_windowed_boundary_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = next(c for c in CORE_CELLS if c.row_id == "windowed-date")
    swap = {("t", "d"): ("t", "d_m"), ("t", "d_m"): ("t", "d")}
    with audit(monkeypatch, attribution=lambda k: swap.get(k, k)) as witness:
        _, tables = run_cell(cell, mx.RESIDENT, tmp_path)
    with pytest.raises(w.WitnessError, match="windowed_date output is not what the column holds"):
        witness.classify(tables)
