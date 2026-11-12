"""AT-P1 to AT-P5 and AT-P7: ``project_tables`` over synthetic facts (no engine run)."""

from __future__ import annotations

import copy
from types import MappingProxyType

import pyarrow as pa
import pytest

from decoy_engine import ExecutionError
from decoy_engine.delivery import TableDelivery as PlanTableDelivery
from decoy_engine.execution import ExecutionResult, RowErrorRecord
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.run_result import (
    CollectedFacts,
    DeliveryFacts,
    TableDelivery,
    _project,
    derive_outcome,
    project_tables,
)

REASON = reason_from_exception(
    ExecutionError(code="out_of_core_insufficient_memory", message="zz_marker_exc")
).to_payload()
NONE = TableDelivery(state="none", format=None, output=None, reason=None, dropped_because=None)


def cfg(*names: str) -> dict:
    return {"tables": [{"name": n, "columns": []} for n in names]}


def run(config, *, execution_result=None, facts=None, delivery=None, run_failed=False):
    """``project_tables`` with every keyword explicit, as the signature demands."""
    return project_tables(
        config=config,
        execution_result=execution_result,
        facts=facts,
        delivery=delivery,
        run_failed=run_failed,
    )


def done(*rows: int) -> ExecutionResult:
    return ExecutionResult(outputs={n: pa.table({"x": list(range(r))}) for n, r in rows})


def by_name(projected):
    return {t.name: t for t in projected.tables}


def entry(state: str, **kw) -> TableDelivery:
    return TableDelivery(**{**NONE.model_dump(), "state": state, **kw})


@pytest.mark.parametrize(
    ("result", "facts", "rows", "source"),
    [
        (done(("a", 3)), CollectedFacts(finished={"a": 7}, rate_rows={"a": 9}), 3, "outputs"),
        (None, CollectedFacts(finished={"a": 7}, rate_rows={"a": 9}), 7, "phase"),
        (None, CollectedFacts(finished={"a": None}, rate_rows={"a": 9}), 9, "rate"),
        (None, CollectedFacts(started=frozenset({"a"})), None, None),
    ],
)
def test_rows_precedence(result, facts, rows, source) -> None:
    table = by_name(run(cfg("a"), execution_result=result, facts=facts))["a"]
    assert (table.rows_out, table.rows_source) == (rows, source)


@pytest.mark.parametrize(
    ("facts", "failed", "expected"),
    [
        (CollectedFacts(aggregate_error=True), True, ["failed", "failed", "failed"]),
        (
            CollectedFacts(aggregate_error=True, finished={"a": 1}),
            True,
            ["succeeded", "failed", "failed"],
        ),
        (CollectedFacts(started=frozenset("abc")), True, ["failed"] * 3),
        (CollectedFacts(started=frozenset("abc")), False, ["succeeded"] * 3),
        (CollectedFacts(), False, ["not_run"] * 3),
        (
            CollectedFacts(finished={"a": 1}, errored=frozenset("b")),
            True,
            ["succeeded", "failed", "not_run"],
        ),
        (None, True, ["not_run"] * 3),
    ],
)
def test_outcomes(facts, failed, expected) -> None:
    projected = run(cfg("a", "b", "c"), facts=facts, run_failed=failed)
    assert [t.outcome for t in projected.tables] == expected
    assert [t.index for t in projected.tables] == [0, 1, 2]


def test_outputs_without_events_succeed_and_count_row_errors() -> None:
    table = by_name(run(cfg("a"), execution_result=done(("a", 2))))["a"]
    assert (table.outcome, table.row_errors) == ("succeeded", 0)


def test_delivery_facts() -> None:
    delivery = DeliveryFacts(
        mode="in_place",
        vault="not_used",
        tables={
            "a": entry("delivered", format="csv", output="a.csv"),
            "b": entry("failed", reason=REASON),
        },
    )
    projected = run(
        cfg("a", "b", "c"),
        execution_result=done(("a", 1), ("b", 2), ("c", 3)),
        delivery=delivery,
    )
    a, b, c = projected.tables
    assert (a.outcome, a.delivery.state, a.delivery.output) == ("succeeded", "delivered", "a.csv")
    assert (b.outcome, b.reason, b.rows_out) == ("failed", REASON, 2)
    assert (c.outcome, c.delivery) == ("succeeded", NONE)
    assert projected.delivery.outcome == "partial"


@pytest.mark.parametrize(
    ("states", "outcome"),
    [
        (("delivered", "delivered"), "delivered"),
        (("delivered", "none"), "partial"),
        (("none", "none"), "nothing"),
        (("failed", "withheld"), "nothing"),
        ((), "not_applicable"),
    ],
)
def test_delivery_outcome_cells(states, outcome) -> None:
    tables = {n: entry(s) for n, s in zip("ab", states, strict=False)}
    got = run(cfg("a", "b"), delivery=DeliveryFacts("strict", "committed", tables))
    assert got.delivery.outcome == outcome
    assert (got.delivery.mode, got.delivery.vault) == ("strict", "committed")


def test_no_delivery_and_preview_are_not_applicable() -> None:
    for delivery in (None, DeliveryFacts.not_applicable()):
        assert run(cfg("a"), delivery=delivery).delivery.outcome == ("not_applicable")


def test_names() -> None:
    projected = run(cfg("ok", "a/b"))
    assert [(t.index, t.name) for t in projected.tables] == [(0, "ok"), (1, None)]


def test_delivery_states_agree() -> None:
    assert isinstance(DeliveryFacts("strict", "not_used", {"a": NONE}).tables["a"], TableDelivery)
    for wrong in (object(), PlanTableDelivery(state="delivered")):
        with pytest.raises(TypeError):
            DeliveryFacts("strict", "not_used", {"a": wrong})
    assert isinstance(DeliveryFacts.not_applicable().tables, MappingProxyType)


def test_delivery_states_project() -> None:
    delivery = DeliveryFacts(
        "best_effort",
        "not_used",
        {
            "a": entry("delivered"),
            "b": entry("failed", reason=REASON),
            "c": entry("withheld"),
            "d": entry("dropped", dropped_because="b"),
        },
    )
    projected = run(cfg("a", "b", "c", "d"), delivery=delivery)
    assert [t.delivery for t in projected.tables] == list(delivery.tables.values())
    assert projected.delivery.outcome == "partial"
    exit_map = {("partially_delivered", None): 6}
    outcome = derive_outcome(
        category=None,
        delivery_mode=projected.delivery.mode,
        delivery_outcome=projected.delivery.outcome,
        blocking_failed=False,
        exit_map=exit_map,
    )
    assert (outcome.status, outcome.exit_code) == ("partially_delivered", 6)


def test_every_keyword_is_required() -> None:
    full = {"config": cfg("a"), "execution_result": None, "facts": None, "delivery": None}
    with pytest.raises(TypeError):
        project_tables(**full)  # no run_failed
    with pytest.raises(TypeError):
        project_tables(**{k: v for k, v in full.items() if k != "delivery"}, run_failed=False)
    assert project_tables(**full, run_failed=False).tables[0].outcome == "not_run"


def test_collected_facts_copy_their_inputs() -> None:
    finished, rate, elapsed = {"a": 1}, {"a": 2}, {"a": 0.5}
    started, errored, phases = {"a"}, {"b"}, [("p", 1.0)]
    facts = CollectedFacts(
        started=started,
        finished=finished,
        errored=errored,
        rate_rows=rate,
        elapsed=elapsed,
        phases=phases,
    )
    finished["z"], rate["z"], elapsed["z"] = 1, 1, 1.0
    started.add("z")
    errored.add("z")
    phases.append(("q", 2.0))
    assert dict(facts.finished) == {"a": 1}
    assert dict(facts.rate_rows) == {"a": 2}
    assert dict(facts.elapsed) == {"a": 0.5}
    assert facts.started == frozenset({"a"})
    assert facts.errored == frozenset({"b"})
    assert facts.phases == (("p", 1.0),)
    with pytest.raises(TypeError):
        facts.finished["y"] = 1  # type: ignore[index]


def test_delivery_facts_own_their_entries() -> None:
    # A caller keeps its entries; changing an entry's reason afterwards must not reach the facts.
    mine = entry("failed", reason=copy.deepcopy(REASON))
    facts = DeliveryFacts("in_place", "not_used", {"a": mine})
    before = run(cfg("a"), delivery=facts).tables[0].to_payload()
    mine.reason["code"] = "zz_changed"
    mine.reason.setdefault("detail", {})["zz"] = 1
    assert facts.tables["a"] is not mine
    assert facts.tables["a"].reason == REASON
    assert run(cfg("a"), delivery=facts).tables[0].to_payload() == before


@pytest.mark.parametrize("bad_state", ["delivered", "failed"])
def test_undeclared_delivery_key_is_refused(bad_state) -> None:
    # A: the adapter keys by a qualified name; B: a ghost failure for a table nobody declared.
    tables = {"a": entry("delivered")} if bad_state == "failed" else {}
    tables["public.a" if bad_state == "delivered" else "ghost"] = entry(bad_state)
    delivery = DeliveryFacts("best_effort", "not_used", tables)
    with pytest.raises(ValueError) as caught:
        run(cfg("a"), delivery=delivery)
    assert str(caught.value) == "delivery names a table the config does not declare"


def test_delivery_facts_refuse_a_non_mapping() -> None:
    for wrong in ([("a", NONE)], None, "a"):
        with pytest.raises(TypeError):
            DeliveryFacts("strict", "not_used", wrong)  # type: ignore[arg-type]


def test_row_errors_are_counted_per_table() -> None:
    def record(table: str, row: int) -> RowErrorRecord:
        return RowErrorRecord(table, "c", row, "t", "r")

    result = ExecutionResult(
        outputs={n: pa.table({"x": [1]}) for n in "abc"},
        row_errors=(record("a", 0), record("a", 1), record("b", 0)),
    )
    got = by_name(run(cfg("a", "b", "c"), execution_result=result))
    assert [got[n].row_errors for n in "abc"] == [2, 1, 0]


def test_kind_follows_the_declared_columns() -> None:
    config = {
        "tables": [
            {"name": "m", "columns": []},
            {"name": "g", "generate_columns": [{"name": "x"}], "row_count": 3},
        ]
    }
    assert [t.kind for t in run(config).tables] == ["mask", "generate"]


def test_an_unknown_kind_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(_project, "classify_table_kinds", lambda config: {"a": "mystery"})
    with pytest.raises(ValueError) as caught:
        run(cfg("a"))
    assert str(caught.value) == "unknown table kind"


@pytest.mark.parametrize(("failed", "outcome"), [(True, "failed"), (False, "succeeded")])
def test_rate_only_table_follows_the_run(failed, outcome) -> None:
    facts = CollectedFacts(rate_rows={"a": 5})
    table = run(cfg("a"), facts=facts, run_failed=failed).tables[0]
    assert (table.outcome, table.rows_out, table.rows_source) == (outcome, 5, "rate")


def test_elapsed_comes_from_the_facts() -> None:
    facts = CollectedFacts(elapsed={"a": 1.25})
    got = by_name(run(cfg("a", "b"), facts=facts))
    assert (got["a"].elapsed_s, got["b"].elapsed_s) == (1.25, None)
