"""GEN-REL E3b owner ruling: a per-parent aggregate's child column must be numeric.

`orders.total` aggregates `lines.amount` per order. Numeric typing keeps a column text while an
aggregate reads it, so a `sequence` or `faker` integer column reaches the aggregate as text and a
`sum` would join the text. Validation refuses a child column that is not numeric under numeric
typing (its declared `output_type`, else the type its generator makes when it stays text) with
`config.generate_aggregate_input_not_numeric`, before any data is read. A numeric child column is
accepted and aggregated; a childless parent's `sum` over it is 0.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from pydantic import ValidationError

from decoy_engine import reasons
from decoy_engine.config._pipeline import PipelineConfig
from tests.unit.generation.relational._support import (
    LINE_ID,
    ORDER_AMOUNT,
    ORDER_ID,
    aggregate_column,
    is_safe,
    per_parent,
    run_route,
)
from tests.unit.generation.unique._gu_e2_support import cfg, ref, table

NOT_NUMERIC = "generate_aggregate_input_not_numeric"
SENTINEL = "SENTINELVALUE"

REFUSED = {
    "sequence": {"name": "amount", "type": "sequence", "start": 1},
    "faker-int": {"name": "amount", "type": "faker", "faker_type": "pyint"},
    "categorical-text": {"name": "amount", "type": "categorical", "categories": ["a", "b"]},
}
ACCEPTED = {
    "distribution": {
        "name": "amount",
        "type": "distribution",
        "family": "uniform",
        "low": 1,
        "high": 50,
    },
    "categorical-number": {"name": "amount", "type": "categorical", "categories": [2, 3, 5]},
    "constant-number": {"name": "amount", "type": "constant", "value": 3},
    "output_type": {"name": "amount", "type": "sequence", "start": 1, "output_type": "int"},
}
# Numeric typing plans no type for these, so only a declared numeric `output_type` makes them
# numeric; without one a text result could reach the aggregate.
UNTYPED = {"name": "amount", "type": "derived", "expression": "lid * 2"}


def _over(amount: dict[str, Any], **options: Any) -> dict[str, Any]:
    return per_parent(line_cols=[LINE_ID, ref("oid", "orders"), amount], **options)


def _errors(config: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    with pytest.raises(ValidationError) as exc:
        PipelineConfig.model_validate(copy.deepcopy(config))
    assert is_safe(exc.value)
    return [(e["type"], tuple(e["loc"])) for e in exc.value.errors()]


def _at_column(column_index: int = 2) -> tuple[Any, ...]:
    return ("tables", 0, "generate_columns", column_index, "column")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


@pytest.mark.parametrize("amount", list(REFUSED.values()), ids=list(REFUSED))
def test_a_child_column_that_is_not_numeric_is_refused_at_validate(amount: dict[str, Any]) -> None:
    assert _errors(_over(amount)) == [(NOT_NUMERIC, _at_column())]


@pytest.mark.parametrize("op", ["sum", "mean"])
def test_the_refusal_holds_for_every_op(op: str) -> None:
    assert _errors(_over(REFUSED["sequence"], op=op)) == [(NOT_NUMERIC, _at_column())]


def test_the_refusal_names_the_table_column_and_aggregate_and_no_value() -> None:
    amount = {"name": "amount", "type": "categorical", "categories": [f"{SENTINEL}a", "b"]}
    with pytest.raises(ValidationError) as exc:
        PipelineConfig.model_validate(_over(amount))
    (error,) = exc.value.errors()
    summary = reasons.get("config", NOT_NUMERIC).summary
    assert error["type"] == NOT_NUMERIC
    assert error["msg"] == f"{summary} Table 'lines', column 'amount', aggregate 'total'."
    assert error["input"] is None
    assert SENTINEL not in str(exc.value) and "categorical" not in error["msg"]
    assert is_safe(exc.value)


@pytest.mark.parametrize("amount", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_a_numeric_child_column_is_accepted_and_aggregated(
    amount: dict[str, Any], tmp_path: Path
) -> None:
    config = _over(amount)
    PipelineConfig.model_validate(copy.deepcopy(config))
    out = run_route("resident", config, tmp_path)
    ids = out["orders"].column("id").to_pylist()
    oids = out["lines"].column("oid").to_pylist()
    values = out["lines"].column("amount").to_pylist()
    want = [sum(v for k, v in zip(oids, values, strict=True) if k == i) for i in ids]
    got = out["orders"].column("total").to_pylist()
    assert all(_is_number(v) for v in got)
    assert [float(v) for v in got] == pytest.approx([float(v) for v in want])


@pytest.mark.parametrize("amount", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_a_childless_parent_sums_to_zero_over_an_accepted_column(
    amount: dict[str, Any], tmp_path: Path
) -> None:
    config = _over(amount, orders=12, lines=4)
    PipelineConfig.model_validate(copy.deepcopy(config))
    out = run_route("resident", config, tmp_path)
    used = set(out["lines"].column("oid").to_pylist())
    totals = out["orders"].column("total").to_pylist()
    childless = [
        t
        for i, t in zip(out["orders"].column("id").to_pylist(), totals, strict=True)
        if i not in used
    ]
    assert childless, "no childless order: the cell would be vacuous"
    assert all(_is_number(t) and t == 0 for t in childless)


def test_a_column_numeric_typing_does_not_type_needs_a_numeric_output_type() -> None:
    assert _errors(_over(UNTYPED)) == [(NOT_NUMERIC, _at_column())]
    PipelineConfig.model_validate(_over({**UNTYPED, "output_type": "decimal(2)"}))


def test_a_date_output_type_is_not_numeric() -> None:
    dated = {"name": "amount", "type": "categorical", "categories": [1, 2], "output_type": "date"}
    assert _errors(_over(dated)) == [(NOT_NUMERIC, _at_column())]


def test_a_reference_child_column_is_judged_by_the_column_it_copies() -> None:
    size = {"name": "size", "type": "categorical", "categories": [1, 2, 3]}
    orders = [ORDER_ID, ORDER_AMOUNT, size, aggregate_column()]
    PipelineConfig.model_validate(_over(ref("amount", "orders", "size"), order_cols=orders))
    copied_counter = _over(ref("amount", "orders", "amount"), order_cols=orders)
    assert _errors(copied_counter) == [(NOT_NUMERIC, _at_column(3))]


@pytest.mark.parametrize(
    "amount", [*REFUSED.values(), *ACCEPTED.values()], ids=[*REFUSED, *ACCEPTED]
)
def test_the_verdict_matches_the_type_numeric_typing_gives_an_aggregate_input(
    amount: dict[str, Any], tmp_path: Path
) -> None:
    """A same-table aggregate reads the column as numeric typing types it for any aggregate."""
    reader = {"name": "n", "type": "derived_aggregate", "op": "count", "column": "amount"}
    out = run_route("resident", cfg([table("lines", 6, [LINE_ID, amount, reader])]), tmp_path)
    arrow_type = out["lines"].schema.field("amount").type
    numeric = (
        pa.types.is_integer(arrow_type)
        or pa.types.is_floating(arrow_type)
        or pa.types.is_decimal(arrow_type)
    )
    try:
        PipelineConfig.model_validate(_over(amount))
        accepted = True
    except ValidationError:
        accepted = False
    assert accepted is numeric


KIND = {"name": "kind", "type": "categorical", "categories": ["x", "y"]}


def _conditional_amount(entry: list[Any]) -> dict[str, Any]:
    return {
        "name": "amount",
        "type": "categorical",
        "categories": [2, 3],
        "condition_on": "kind",
        "by": {"x": {"categories": entry}},
    }


def test_a_conditional_categorical_child_is_judged_by_every_category_it_draws(
    tmp_path: Path,
) -> None:
    numbers = per_parent(line_cols=[LINE_ID, ref("oid", "orders"), KIND, _conditional_amount([7])])
    PipelineConfig.model_validate(copy.deepcopy(numbers))
    out = run_route("resident", numbers, tmp_path)
    assert set(out["lines"].column("amount").to_pylist()) <= {2, 3, 7}
    assert all(_is_number(v) for v in out["orders"].column("total").to_pylist())
    text = per_parent(line_cols=[LINE_ID, ref("oid", "orders"), KIND, _conditional_amount(["a"])])
    assert _errors(text) == [(NOT_NUMERIC, _at_column(3))]
