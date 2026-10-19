"""Phase F / F-audit (C): the vacuity fence.

A value-equality witness over an empty comparison set is true of anything. A positive consumption
claim therefore needs at least one ACTUAL non-null, comparable output position. `live_positions()`
in the witness drops mask-nulls only, so an intrinsic null (an empty reference parent, a null
windowed_date anchor) would still pass without the guard in `_f0_witness._require_nonvacuous`.

Three cases are tested separately and none may earn a positive label: zero rows, probability-one
null masking, and intrinsic-null output. The schema contract is asserted too: a core run uses the
DECLARED schema from `classify_plan` (`admission.schema`, a typed empty table), never an inferred
Arrow null; a legacy run keeps the legacy inference, which is the documented difference.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.generation import _column_streams
from decoy_engine.generation._admission import classify_plan
from tests.parity import _f0_fixtures as fx
from tests.parity import _f0_witness as w
from tests.parity import _f_audit_cells as cells
from tests.parity import _f_audit_matrix as mx
from tests.parity._f_audit_support import BoundedSink, forbid_legacy, run_cell
from tests.parity._f_audit_witness import AuditWitness, audit
from tests.parity._fr0_support import prepared_config

VACUOUS = "no live non-null position|no output was recorded|no observable backend event"
ANCHOR = {"name": "anchor", "type": "categorical", "categories": cells.ANCHORS}
KINDS: dict[str, dict[str, Any]] = {
    "sequence": {"type": "sequence", "start": 1},
    "categorical": {"type": "categorical", "categories": ["a", "b"]},
    "faker": {"type": "faker", "faker_type": "city", "pooled": False},
    "windowed_date": {
        "type": "windowed_date",
        "anchor": "anchor",
        "min_days": 1,
        "max_days": 30,
    },
}
DECLARED = {
    "sequence": pa.string(),
    "categorical": pa.string(),
    "faker": pa.string(),
    "windowed_date": pa.string(),
}


def _config(kind: str, rows: int, **extra: Any) -> dict[str, Any]:
    cols = [ANCHOR, {"name": "c", **KINDS[kind], **extra}]
    return fx.config([fx.table("t", rows, cols)])


def _cell(config: dict[str, Any]) -> cells.Cell:
    return cells.Cell("fence", lambda: config, {})


def _classify(witness: AuditWitness, tables: dict[str, pa.Table], table: str, col: str) -> Any:
    return witness._classify_one((table, col), tables[table].column(col).to_pylist())


@pytest.mark.parametrize("route", [mx.RESIDENT, mx.STREAMED])
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_zero_rows_keep_the_declared_schema_and_earn_no_label(
    kind: str, route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(kind, 0)
    forbid_legacy(monkeypatch)
    with audit(monkeypatch) as witness:
        _, tables = run_cell(_cell(config), route, tmp_path)
    assert tables["t"].num_rows == 0
    declared = classify_plan(prepared_config(config)).schema("t")
    assert tables["t"].schema.field("c").type == DECLARED[kind] == declared.field("c").type
    assert not pa.types.is_null(tables["t"].schema.field("c").type)
    with pytest.raises(w.WitnessError, match=VACUOUS):
        _classify(witness, tables, "t", "c")


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_probability_one_masking_earns_no_label_and_keeps_the_declared_type(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(kind, 30, null_probability=1.0)
    forbid_legacy(monkeypatch)
    with audit(monkeypatch) as witness:
        _, tables = run_cell(_cell(config), mx.RESIDENT, tmp_path)
    column = tables["t"].column("c")
    assert column.null_count == 30
    assert column.type == DECLARED[kind], "core all-null output keeps its declared type"
    with pytest.raises(w.WitnessError, match=VACUOUS):
        _classify(witness, tables, "t", "c")


def test_the_all_null_mutant_would_pass_without_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: with the guard disabled the all-null column is (wrongly) labelled."""
    config = _config("categorical", 30, null_probability=1.0)
    with audit(monkeypatch) as witness:
        _, tables = run_cell(_cell(config), mx.RESIDENT, tmp_path)
    with monkeypatch.context() as unguarded:
        unguarded.setattr(AuditWitness, "_require_nonvacuous", staticmethod(lambda *a: None))
        report = _classify(witness, tables, "t", "c")
    assert report.selection == {w.PYTHON_RNG}, "vacuously true, which is the hole the guard closes"
    with pytest.raises(w.WitnessError, match=VACUOUS):
        _classify(witness, tables, "t", "c")


def test_a_mutant_that_nulls_a_live_column_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 30 percent mask hollowed out to all-null: the witness sees every row nulled, so the
    value comparison has nothing left and would pass vacuously without the guard."""
    config = _config("categorical", 30, null_probability=0.3)

    def hollow(inner: Any) -> Any:
        def next_batch_from(self: Any, offset: int, size: int, siblings: Any) -> list[Any]:
            inner(self, offset, size, siblings)
            return [None] * size

        return next_batch_from

    with audit(monkeypatch) as witness:
        monkeypatch.setattr(
            _column_streams.NullMaskedStream,
            "next_batch_from",
            hollow(_column_streams.NullMaskedStream.next_batch_from),
        )
        _, tables = run_cell(_cell(config), mx.RESIDENT, tmp_path)
    assert tables["t"].column("c").null_count == 30
    with monkeypatch.context() as unguarded:
        unguarded.setattr(AuditWitness, "_require_nonvacuous", staticmethod(lambda *a: None))
        assert _classify(witness, tables, "t", "c").selection == {w.PYTHON_RNG}
    with pytest.raises(w.WitnessError, match=VACUOUS):
        _classify(witness, tables, "t", "c")


@pytest.mark.parametrize("dist", ["sequential", "random", "weighted"])
@pytest.mark.parametrize("route", [mx.RESIDENT, mx.STREAMED])
def test_an_empty_reference_parent_is_core_intrinsically_null_and_earns_no_label(
    dist: str, route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = mx.row("reference-empty-parent")
    assert (row.route, row.compute, row.witness) == (mx.CORE, mx.NO_SELECTION, mx.EXCLUDED)
    cell = cells.Cell("fence", lambda: cells.cfg_reference(dist, parent_null=1.0), {})
    forbid_legacy(monkeypatch)
    with audit(monkeypatch) as witness:
        result, tables = run_cell(cell, route, tmp_path)
    assert result.mode == route and {("child", "r"), ("par", "k")} <= witness.core_keys
    child = tables["child"]
    assert child.column("r").null_count == child.num_rows > 0
    assert child.schema.field("r").type == pa.string(), "declared type, not inferred null"
    with pytest.raises(w.WitnessError, match=VACUOUS):
        _classify(witness, tables, "child", "r")


@pytest.mark.parametrize("route", [mx.RESIDENT, mx.STREAMED])
def test_a_null_windowed_anchor_is_an_intrinsic_null_not_a_witnessed_date(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = {**ANCHOR, "null_probability": 1.0}
    win = {"name": "d", **KINDS["windowed_date"]}
    cell = cells.Cell("fence", lambda: fx.config([fx.table("t", 30, [anchor, win])]), {})
    forbid_legacy(monkeypatch)
    with audit(monkeypatch) as witness:
        _, tables = run_cell(cell, route, tmp_path)
    assert tables["t"].column("d").null_count == 30
    assert tables["t"].schema.field("d").type == pa.string()
    with pytest.raises(w.WitnessError, match=VACUOUS):
        _classify(witness, tables, "t", "d")


def test_legacy_plans_keep_their_legacy_schema_for_all_null_and_zero_row_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy generator still infers Arrow null from values; only the core declares types."""
    from decoy_engine.generation import _plan_entry
    from tests.parity._fr0_support import plan_of

    cols = [
        cells.cfg_formula()["tables"][0]["generate_columns"][0],
        {"name": "allnull", "type": "categorical", "categories": ["a"], "null_probability": 1.0},
    ]
    config = fx.config(
        [fx.table("t", 20, cols), fx.table("empty", 0, [KINDS["sequence"] | {"name": "s"}])]
    )
    result = _plan_entry.generate_tables_streamed(plan_of(config), sink=BoundedSink())
    assert result.reason.startswith("legacy:")
    assert result.tables is not None
    assert pa.types.is_null(result.tables["t"].schema.field("allnull").type)
    assert pa.types.is_null(result.tables["empty"].schema.field("s").type)
    core = classify_plan(prepared_config(_config("categorical", 20, null_probability=1.0)))
    assert core.schema("t").field("c").type == pa.string()
