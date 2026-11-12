"""AT-P1 to AT-P5 and AT-P7: ``project_tables`` over synthetic facts (no engine run)."""

from __future__ import annotations

from types import MappingProxyType

import pyarrow as pa
import pytest

from decoy_engine.delivery import TableDelivery as PlanTableDelivery
from decoy_engine.errors import ExecutionError
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.run_result import (
    CollectedFacts,
    DeliveryFacts,
    TableDelivery,
    derive_outcome,
    project_tables,
)

REASON = reason_from_exception(
    ExecutionError(code="out_of_core_insufficient_memory", message="zz_marker_exc")
).to_payload()
NONE = TableDelivery(state="none", format=None, output=None, reason=None, dropped_because=None)


def cfg(*names: str) -> dict:
    return {"tables": [{"name": n, "columns": []} for n in names]}


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
    table = by_name(project_tables(config=cfg("a"), execution_result=result, facts=facts))["a"]
    assert (table.rows_out, table.rows_source) == (rows, source)


@pytest.mark.parametrize(
    ("facts", "failed", "expected"),
    [
        (CollectedFacts(aggregate_error=True), True, ["failed", "failed", "failed"]),
        (CollectedFacts(aggregate_error=True, finished={"a": 1}), True,
         ["succeeded", "failed", "failed"]),
        (CollectedFacts(started=frozenset("abc")), True, ["failed"] * 3),
        (CollectedFacts(started=frozenset("abc")), False, ["succeeded"] * 3),
        (CollectedFacts(), False, ["not_run"] * 3),
        (CollectedFacts(finished={"a": 1}, errored=frozenset("b")), True,
         ["succeeded", "failed", "not_run"]),
        (None, True, ["not_run"] * 3),
    ],
)
def test_outcomes(facts, failed, expected) -> None:
    projected = project_tables(config=cfg("a", "b", "c"), facts=facts, run_failed=failed)
    assert [t.outcome for t in projected.tables] == expected
    assert [t.index for t in projected.tables] == [0, 1, 2]


def test_outputs_without_events_succeed_and_count_row_errors() -> None:
    table = by_name(project_tables(config=cfg("a"), execution_result=done(("a", 2))))["a"]
    assert (table.outcome, table.row_errors) == ("succeeded", 0)


def test_delivery_facts() -> None:
    delivery = DeliveryFacts(
        mode="in_place",
        vault="not_used",
        tables={"a": entry("delivered", format="csv", output="a.csv"), "b": entry("failed", reason=REASON)},
    )
    projected = project_tables(
        config=cfg("a", "b", "c"), execution_result=done(("a", 1), ("b", 2), ("c", 3)),
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
    got = project_tables(config=cfg("a", "b"), delivery=DeliveryFacts("strict", "committed", tables))
    assert got.delivery.outcome == outcome
    assert (got.delivery.mode, got.delivery.vault) == ("strict", "committed")


def test_no_delivery_and_preview_are_not_applicable() -> None:
    for delivery in (None, DeliveryFacts.not_applicable()):
        assert project_tables(config=cfg("a"), delivery=delivery).delivery.outcome == (
            "not_applicable"
        )


def test_names() -> None:
    projected = project_tables(config=cfg("ok", "a/b"))
    assert [(t.index, t.name) for t in projected.tables] == [(0, "ok"), (1, None)]


def test_delivery_states_agree() -> None:
    assert isinstance(DeliveryFacts("strict", "not_used", {"a": NONE}).tables["a"], TableDelivery)
    for wrong in (object(), PlanTableDelivery(state="delivered")):
        with pytest.raises(TypeError):
            DeliveryFacts("strict", "not_used", {"a": wrong})
    assert isinstance(DeliveryFacts.not_applicable().tables, MappingProxyType)


def test_delivery_states_project() -> None:
    delivery = DeliveryFacts(
        "best_effort", "not_used",
        {
            "a": entry("delivered"),
            "b": entry("failed", reason=REASON),
            "c": entry("withheld"),
            "d": entry("dropped", dropped_because="b"),
        },
    )
    projected = project_tables(config=cfg("a", "b", "c", "d"), delivery=delivery)
    assert [t.delivery for t in projected.tables] == list(delivery.tables.values())
    assert projected.delivery.outcome == "partial"
    exit_map = {("partially_delivered", None): 6}
    outcome = derive_outcome(
        category=None, delivery_mode=projected.delivery.mode,
        delivery_outcome=projected.delivery.outcome, blocking_failed=False, exit_map=exit_map,
    )
    assert (outcome.status, outcome.exit_code) == ("partially_delivered", 6)
