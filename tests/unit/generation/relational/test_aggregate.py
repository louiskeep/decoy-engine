"""GEN-REL E3b: a parent column aggregates its own children (AT-12, aggregate AT-16).

`orders.total` is `op` over `lines.amount` of the lines whose `oid` points at that order: SQL
`GROUP BY` over the child with a `LEFT JOIN` from the parent. The oracle here is the same query
written with pandas `merge(how="left")` and `groupby`. A childless order gets 0 for `sum` and
`count` and a null for `mean`, `min` and `max`, never NaN. `orders` also has its own unrelated
`amount`, which the aggregate must never read and whose numeric type it must not change.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest
from pydantic import ValidationError

from decoy_engine import reasons
from decoy_engine.config import _generate_options as registry
from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.generation._reproducibility import REASON_DEPENDS, reproducibility_exclusions
from tests.unit.generation.relational._support import (
    AGGREGATE_OPS,
    LINE_AMOUNT,
    LINE_ID,
    ORDER_AMOUNT,
    ORDER_ID,
    SECOND_SEED,
    aggregate_column,
    digest,
    is_safe,
    per_parent,
    run_route,
)
from tests.unit.generation.unique._gu_e2_support import cfg, ref, table

READ_BEFORE_FILL = "generate_aggregate_read_before_fill"
LOOKUP_KEY = "generate_parent_lookup_key_not_unique"
LOOKUP_SELF = "generate_parent_lookup_self_reference"
SENTINEL_KEY = "SENTINELKEY"
IDENTITY = {"sum": 0, "count": 0, "mean": None, "min": None, "max": None}


def _totals(out: dict[str, pa.Table]) -> list[Any]:
    return out["orders"].column("total").to_pylist()


def _oracle(out: dict[str, pa.Table], op: str) -> list[Any]:
    """`op` per order over a left join of `orders` to its `lines`, nulls as None."""
    ids = out["orders"].column("id").to_pylist()
    lines = pd.DataFrame(
        {
            "oid": out["lines"].column("oid").to_pylist(),
            "amount": out["lines"].column("amount").to_pylist(),
        },
        dtype=object,
    )
    lines = lines[lines["oid"].notna()]
    joined = pd.DataFrame({"oid": ids}, dtype=object).merge(lines, on="oid", how="left")
    joined["amount"] = pd.to_numeric(joined["amount"])
    per_order = joined.groupby("oid", sort=False)["amount"].agg(op).reindex(ids)
    return [None if pd.isna(v) else v for v in per_order.tolist()]


def _same(got: list[Any], want: list[Any]) -> bool:
    if len(got) != len(want):
        return False
    for g, w in zip(got, want, strict=True):
        if (g is None) != (w is None):
            return False
        if g is not None and not math.isclose(g, w, rel_tol=1e-12):
            return False
    return True


def _childless(out: dict[str, pa.Table]) -> list[int]:
    """Rows of `orders` that no line points at."""
    used = {v for v in out["lines"].column("oid").to_pylist() if v is not None}
    return [i for i, v in enumerate(out["orders"].column("id").to_pylist()) if v not in used]


def test_the_fixture_has_childless_orders_null_keys_and_null_amounts(tmp_path: Path) -> None:
    out = run_route("resident", per_parent(), tmp_path)
    oids = out["lines"].column("oid").to_pylist()
    amounts = out["lines"].column("amount").to_pylist()
    assert _childless(out), "no childless order: the identity cells would be vacuous"
    assert None in oids and None in amounts
    counts = pd.Series([v for v in oids if v is not None]).value_counts()
    assert counts.max() >= 2, "no order with several lines"


# ---- AT-12: every op over each parent's own children -------------------------------------------


@pytest.mark.parametrize("op", AGGREGATE_OPS)
def test_at12_each_op_matches_a_pandas_groupby_over_a_left_join(op: str, tmp_path: Path) -> None:
    out = run_route("resident", per_parent(op), tmp_path)
    assert _same(_totals(out), _oracle(out, op)), op


@pytest.mark.parametrize("op", AGGREGATE_OPS)
def test_at12_a_childless_parent_gets_the_identity_of_its_op(op: str, tmp_path: Path) -> None:
    out = run_route("resident", per_parent(op), tmp_path)
    totals = _totals(out)
    childless = [totals[i] for i in _childless(out)]
    assert childless == [IDENTITY[op]] * len(childless)
    if IDENTITY[op] is not None:
        assert all(type(v) is int for v in childless)


@pytest.mark.parametrize("op", AGGREGATE_OPS)
def test_at12_no_value_is_nan_and_every_missing_value_is_a_null(op: str, tmp_path: Path) -> None:
    out = run_route("resident", per_parent(op), tmp_path)
    column = out["orders"].column("total")
    values = column.to_pylist()
    assert not any(isinstance(v, float) and math.isnan(v) for v in values)
    assert column.null_count == values.count(None)
    # COALESCE to 0 on the additive ops only: a childless order has a null mean, min and max.
    if op in ("mean", "min", "max"):
        assert column.null_count >= 1
    else:
        assert column.null_count == 0


@pytest.mark.parametrize("op", AGGREGATE_OPS)
def test_at12_a_parent_whose_children_hold_only_nulls_gets_no_nan(op: str, tmp_path: Path) -> None:
    """pandas gives NaN for the mean, min and max of an all-null group; the column holds a null."""
    nulls = {**LINE_AMOUNT, "null_probability": 1.0}
    out = run_route(
        "resident", per_parent(op, line_cols=[LINE_ID, ref("oid", "orders"), nulls]), tmp_path
    )
    values = _totals(out)
    with_children = [values[i] for i in range(len(values)) if i not in _childless(out)]
    assert with_children, "no order has a line: the cell would be vacuous"
    assert not any(isinstance(v, float) and math.isnan(v) for v in values)
    assert with_children == [0 if op in ("sum", "count") else None] * len(with_children)
    assert _same(values, _oracle(out, op))


@pytest.mark.parametrize(
    ("op", "arrow_type"),
    [
        ("sum", pa.int64()),
        ("count", pa.int64()),
        ("mean", pa.float64()),
        ("min", pa.int64()),
        ("max", pa.int64()),
    ],
)
def test_at12_the_returned_parent_table_holds_the_typed_filled_values(
    op: str, arrow_type: pa.DataType, tmp_path: Path
) -> None:
    out = run_route("resident", per_parent(op), tmp_path)
    assert out["orders"].schema.field("total").type == arrow_type
    assert out["orders"].column("total").null_count < out["orders"].num_rows


def test_at12_an_output_type_on_the_aggregate_is_applied_to_the_filled_values(
    tmp_path: Path,
) -> None:
    config = per_parent("sum", output_type="decimal(2)")
    PipelineConfig.model_validate(config)
    out = run_route("resident", config, tmp_path)
    assert pa.types.is_decimal(out["orders"].schema.field("total").type)
    assert _same([float(v) for v in _totals(out)], _oracle(out, "sum"))


def test_at12_the_unrelated_parent_amount_keeps_its_numeric_arrow_type(tmp_path: Path) -> None:
    out = run_route("resident", per_parent("sum"), tmp_path)
    assert out["orders"].schema.field("amount").type == pa.int64()
    assert out["orders"].column("amount").to_pylist() == list(range(100, 110))
    assert _same(_totals(out), _oracle(out, "sum"))


def test_at12_a_parent_with_no_same_named_column_is_aggregated_the_same_way(
    tmp_path: Path,
) -> None:
    with_amount = run_route("resident", per_parent("sum"), tmp_path)
    without = per_parent("sum", order_cols=[ORDER_ID, aggregate_column("sum")])
    out = run_route("resident", without, tmp_path)
    assert _totals(out) == _totals(with_amount)
    assert digest(out["lines"]) == digest(with_amount["lines"])


def test_at12_the_other_parent_columns_and_the_child_keep_their_bytes(tmp_path: Path) -> None:
    """Adding the aggregate changes no other column: it is filled after both tables exist."""
    plain = per_parent(order_cols=[ORDER_ID, ORDER_AMOUNT])
    before = run_route("resident", plain, tmp_path)
    after = run_route("resident", per_parent("sum"), tmp_path)
    assert digest(after["lines"]) == digest(before["lines"])
    assert digest(after["orders"].select(["id", "amount"])) == digest(before["orders"])
    assert _same(_totals(after), _oracle(after, "sum"))


def test_at12_the_aggregate_reads_the_finished_child_table(tmp_path: Path) -> None:
    """A child column declared after the foreign key, and nulled after its draw, is read as
    the child table holds it."""
    kids = [LINE_ID, LINE_AMOUNT, ref("oid", "orders", null_probability=0.1)]
    out = run_route("resident", per_parent("count", line_cols=kids), tmp_path)
    assert _same(_totals(out), _oracle(out, "count"))


def test_the_post_pass_leaves_tables_without_a_per_parent_aggregate_untouched() -> None:
    from decoy_engine.generation.relational._aggregate import with_aggregates

    config = per_parent(order_cols=[ORDER_ID, ORDER_AMOUNT])
    tables = {"orders": pa.table({"id": [1]}), "lines": pa.table({"lid": [1]})}
    assert with_aggregates(config, tables) is tables


# ---- aggregate AT-16: the same seed gives the same aggregates -----------------------------------


def test_at16_the_same_seed_gives_the_same_aggregates_and_another_seed_others(
    tmp_path: Path,
) -> None:
    first = run_route("resident", per_parent("sum"), tmp_path)
    again = run_route("resident", per_parent("sum"), tmp_path)
    other = run_route("resident", per_parent("sum", seed=SECOND_SEED), tmp_path)
    assert digest(first["orders"]) == digest(again["orders"])
    assert _totals(first) != _totals(other)
    assert _same(_totals(other), _oracle(other, "sum"))


# ---- validate: the lookup key, the companion keys and nulls -------------------------------------


def _errors(config: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    with pytest.raises(ValidationError) as exc:
        PipelineConfig.model_validate(copy.deepcopy(config))
    assert is_safe(exc.value)
    return [(e["type"], tuple(e["loc"])) for e in exc.value.errors()]


def _at(option: str, table_index: int = 0, column_index: int = 2) -> tuple[Any, ...]:
    return ("tables", table_index, "generate_columns", column_index, option)


def test_at12_a_valid_per_parent_aggregate_validates() -> None:
    for op in AGGREGATE_OPS:
        PipelineConfig.model_validate(per_parent(op))


@pytest.mark.parametrize(
    "parent_key",
    [
        {"name": "id", "type": "categorical", "categories": [1, 2, 3]},
        {"name": "id", "type": "sequence", "start": 1, "null_probability": 0.1},
        {"name": "id", "type": "faker", "faker_type": "pyint"},
    ],
    ids=["repeated", "nullable", "not-a-sequence"],
)
def test_at12_a_parent_key_that_is_not_a_non_null_sequence_is_refused(
    parent_key: dict[str, Any],
) -> None:
    config = per_parent(order_cols=[parent_key, ORDER_AMOUNT, aggregate_column()])
    assert _errors(config) == [(LOOKUP_KEY, _at("via"))]


def test_at12_the_parent_key_must_be_declared_before_the_aggregate() -> None:
    config = per_parent(order_cols=[ORDER_AMOUNT, aggregate_column(), ORDER_ID])
    assert _errors(config) == [(LOOKUP_KEY, _at("via", column_index=1))]


def test_at12_via_must_reference_this_parent_table() -> None:
    shops = table("shops", 3, [{"name": "id", "type": "sequence", "start": 1}])
    kids = [LINE_ID, ref("oid", "orders"), ref("sid", "shops"), LINE_AMOUNT]
    config = per_parent(line_cols=kids, via="sid")
    config["tables"].append(shops)
    assert _errors(config) == [(LOOKUP_KEY, _at("via"))]


def test_at12_via_must_be_a_reference_column() -> None:
    assert _errors(per_parent(via="lid")) == [(LOOKUP_KEY, _at("via"))]


def test_at12_via_through_a_self_reference_is_refused() -> None:
    boss = ref("boss", "orders", "id")
    config = per_parent(order_cols=[ORDER_ID, boss, aggregate_column(from_table="orders")])
    config["tables"][0]["generate_columns"][2]["via"] = "boss"
    config["tables"][0]["generate_columns"][2]["column"] = "id"
    assert _errors(config) == [(LOOKUP_SELF, _at("via"))]


def test_at12_via_needs_from_table_and_from_table_needs_via() -> None:
    lonely_via = {"name": "total", "type": "derived_aggregate", "op": "sum"}
    lonely_via.update(column="amount", via="oid")
    config = per_parent(order_cols=[ORDER_ID, ORDER_AMOUNT, lonely_via])
    assert _errors(config) == [("generate_option_unknown", _at("via"))]
    no_via = aggregate_column()
    del no_via["via"]
    config = per_parent(order_cols=[ORDER_ID, ORDER_AMOUNT, no_via])
    assert _errors(config) == [("missing", _at("via"))]


def test_at12_null_probability_on_a_per_parent_aggregate_is_refused() -> None:
    config = per_parent(null_probability=0.2)
    assert _errors(config) == [("generate_per_parent_null_conflict", _at("null_probability"))]
    PipelineConfig.model_validate(per_parent(null_probability=0))


def test_at15_validate_refusals_of_a_per_parent_aggregate_carry_the_fixed_summary_only() -> None:
    key = {"name": "id", "type": "categorical", "categories": [f"{SENTINEL_KEY}a", "b"]}
    config = per_parent(order_cols=[key, ORDER_AMOUNT, aggregate_column()])
    with pytest.raises(ValidationError) as exc:
        PipelineConfig.model_validate(config)
    (error,) = exc.value.errors()
    assert error["msg"] == reasons.get("config", LOOKUP_KEY).summary
    assert error["input"] is None
    assert SENTINEL_KEY not in str(exc.value) and is_safe(exc.value)


# ---- validate: nothing reads a per-parent aggregate before the post-pass fills it --------------


def _same_table_reader(reader: dict[str, Any]) -> tuple[dict[str, Any], tuple[int, int]]:
    config = per_parent()
    config["tables"][0]["generate_columns"].append(reader)
    return config, (0, 3)


def _child_reader(reader: dict[str, Any]) -> tuple[dict[str, Any], tuple[int, int]]:
    config = per_parent()
    config["tables"][1]["generate_columns"].append(reader)
    return config, (1, 3)


def _grandparent_reader(target: str) -> tuple[dict[str, Any], tuple[int, int]]:
    """`customers.spend` aggregates the `orders` column `target` of each customer's orders."""
    config = per_parent()
    config["tables"][0]["generate_columns"].insert(1, ref("cust", "customers"))
    # An aggregate's input must hold numbers, so the parent's counter declares an int type.
    config["tables"][0]["generate_columns"][2] = {**ORDER_AMOUNT, "output_type": "int"}
    spend = {"name": "spend", "type": "derived_aggregate", "op": "sum", "column": target}
    spend.update(from_table="orders", via="cust")
    customers = table("customers", 4, [{"name": "id", "type": "sequence", "start": 1}, spend])
    config["tables"].insert(0, customers)
    return config, (0, 1)


# Each realistic reader of an `orders` column, built to read the column `target`, with the
# option that names it.
READERS: dict[str, tuple[str, Any]] = {
    "derived.expression": (
        "expression",
        lambda t: _same_table_reader({"name": "r", "type": "derived", "expression": f"{t} + 1"}),
    ),
    "formula.references": (
        "references",
        lambda t: _same_table_reader(
            {"name": "r", "type": "formula", "formula": t, "references": [t]}
        ),
    ),
    "grouped_series.group_by": (
        "group_by",
        lambda t: _same_table_reader(
            {"name": "r", "type": "grouped_series", "group_by": t, "order_by": "id"}
        ),
    ),
    "grouped_series.order_by": (
        "order_by",
        lambda t: _same_table_reader(
            {"name": "r", "type": "grouped_series", "group_by": "id", "order_by": t}
        ),
    ),
    "group_key.group_by": (
        "group_by",
        lambda t: _same_table_reader({"name": "r", "type": "group_key", "group_by": t}),
    ),
    "windowed_date.anchor": (
        "anchor",
        lambda t: _same_table_reader(
            {"name": "r", "type": "windowed_date", "anchor": t, "max_days": 3}
        ),
    ),
    "statistical.condition_on": (
        "condition_on",
        lambda t: _same_table_reader(
            {"name": "r", "type": "statistical", "snapshot_file": "s.json", "condition_on": t}
        ),
    ),
    "derived_aggregate.column (scalar)": (
        "column",
        lambda t: _same_table_reader(
            {"name": "r", "type": "derived_aggregate", "op": "max", "column": t}
        ),
    ),
    "derived_aggregate.column (per-parent)": ("column", _grandparent_reader),
    "windowed_date.anchor (anchor_via)": (
        "anchor",
        lambda t: _child_reader(
            {"name": "r", "type": "windowed_date", "anchor": t, "anchor_via": "oid", "max_days": 3}
        ),
    ),
    "reference.reference_column": (
        "reference_column",
        lambda t: _child_reader(ref("r", "orders", t)),
    ),
}


@pytest.mark.parametrize("reader", sorted(READERS))
def test_at12_each_reader_of_a_per_parent_aggregate_is_refused_before_fill(reader: str) -> None:
    option, build = READERS[reader]
    config, (t_idx, c_idx) = build("total")
    assert _errors(config) == [(READ_BEFORE_FILL, _at(option, t_idx, c_idx))]


@pytest.mark.parametrize("reader", sorted(READERS))
def test_at12_the_same_reader_of_an_ordinary_column_validates(reader: str) -> None:
    """The refusal is about the aggregate, not the reader: aimed at the parent's own `amount`
    beside the same aggregate, the config validates."""
    _, build = READERS[reader]
    config, _ = build("amount")
    PipelineConfig.model_validate(config)


def test_at12_a_new_column_input_is_refused_before_fill_without_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An option registered later with a reference kind reaches the refusal through the one
    resolver: nothing lists the readers by name."""
    spec = registry.GENERATE_OPTIONS["group_key"]
    probe = registry.OptionSpec(ref_kind="column")
    patched = registry.TypeOptions({**spec.keys, "probe_on": probe}, required=spec.required)
    monkeypatch.setitem(registry.GENERATE_OPTIONS, "group_key", patched)  # type: ignore[index]
    config, _ = _same_table_reader(
        {"name": "r", "type": "group_key", "group_by": "id", "probe_on": "total"}
    )
    assert _errors(config) == [(READ_BEFORE_FILL, _at("probe_on", 0, 3))]


def test_at15_the_read_before_fill_refusal_carries_the_fixed_summary_only() -> None:
    config, _ = _same_table_reader({"name": "r", "type": "derived", "expression": "total + 1"})
    with pytest.raises(ValidationError) as exc:
        PipelineConfig.model_validate(config)
    (error,) = exc.value.errors()
    assert error["type"] == READ_BEFORE_FILL
    assert error["msg"] == reasons.get("config", READ_BEFORE_FILL).summary
    assert error["input"] is None and is_safe(exc.value)


# ---- reproducibility: the inputs are the child's column and foreign key ------------------------


def test_an_aggregate_over_a_child_column_that_is_not_reproducible_is_excluded() -> None:
    fresh = {**LINE_AMOUNT, "determinism": "fresh"}
    config = per_parent(line_cols=[LINE_ID, ref("oid", "orders"), fresh])
    assert reproducibility_exclusions(config)[("orders", "total")] == REASON_DEPENDS


def test_an_aggregate_through_a_foreign_key_that_is_not_reproducible_is_excluded() -> None:
    config = per_parent(line_cols=[LINE_ID, ref("oid", "orders", determinism="fresh"), LINE_AMOUNT])
    assert reproducibility_exclusions(config)[("orders", "total")] == REASON_DEPENDS


def test_a_same_named_parent_column_is_not_the_aggregates_input() -> None:
    fresh_parent_amount = {**ORDER_AMOUNT, "determinism": "fresh"}
    config = per_parent(order_cols=[ORDER_ID, fresh_parent_amount, aggregate_column()])
    excluded = reproducibility_exclusions(config)
    assert ("orders", "amount") in excluded
    assert ("orders", "total") not in excluded


def test_a_scalar_aggregate_still_depends_on_its_sibling() -> None:
    fresh = {**ORDER_AMOUNT, "determinism": "fresh"}
    scalar = {"name": "total", "type": "derived_aggregate", "op": "sum", "column": "amount"}
    config = cfg([table("orders", 4, [ORDER_ID, fresh, scalar])])
    assert reproducibility_exclusions(config)[("orders", "total")] == REASON_DEPENDS
