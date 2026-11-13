"""SP-10: integration tests for the derived strategy handler (B1 / TDD).

B1 proved: a plan with ``strategy: derived`` must be reachable through
``_pandas_adapter`` (via SCALAR_HANDLERS), not only via calling
``apply_derived()`` directly.

These tests drive a real ``ColumnSeed`` with ``strategy: "derived"``
through ``PandasExecutionAdapter.run_single``, confirming:
  - The strategy is registered in SCALAR_HANDLERS.
  - Mask mode and gen mode both work (no code branching; derived is a pure
    function of the row context, so both modes are identical in behavior).
  - All null_propagation modes work end-to-end.
  - Bounds clip end-to-end.
  - Determinism: same plan + same table -> same output.
  - SCALAR_HANDLERS does NOT hit MASK_UNKNOWN_STRATEGY.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = b"\xca\xfe" * 4  # 8 bytes


def _col(
    strategy: str,
    *,
    provider_config: tuple[tuple[str, Any], ...] = (),
    namespace: str | None = None,
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider="x_nobackend",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(per_table: list[tuple[str, TableSeed]]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=tuple(per_table),
        )
    )


def _run(
    columns: dict[str, Any],
    per_column: list[tuple[str, ColumnSeed]],
) -> dict[str, list]:
    """Run the adapter with the given table and per-column plan; return {col: values}."""
    arrow_types = {}
    for col, vals in columns.items():
        if vals and isinstance(vals[0], (int, float)) and vals[0] is not None:
            arrow_types[col] = pa.float64()
        else:
            arrow_types[col] = pa.string()
    arrays = {col: pa.array(vals, type=arrow_types.get(col)) for col, vals in columns.items()}
    table = pa.table(arrays)
    plan = _plan([("t", TableSeed(per_column=tuple(per_column), per_group=()))])
    result = PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    return {col: result.output.column(col).to_pylist() for col in columns}


class TestDerivedAdapterIntegration:
    """B1: derived is reachable through a real plan -> _pandas_adapter path."""

    def test_derived_mask_mode_reachable_via_adapter(self) -> None:
        """strategy: derived is registered in SCALAR_HANDLERS and computes
        values from row context when driven through PandasExecutionAdapter."""
        out = _run(
            {"a": [1.0, 2.0, 3.0], "b": [10.0, 20.0, 30.0]},
            [
                ("a", _col("passthrough")),
                (
                    "b",
                    _col(
                        "derived",
                        provider_config=(("expression", "a + 10"),),
                    ),
                ),
            ],
        )
        # b should be a + 10: 11, 12, 13
        assert out["b"][0] == 11.0
        assert out["b"][1] == 12.0
        assert out["b"][2] == 13.0

    def test_row_context_is_columns_not_the_frame_index(self) -> None:
        # The per-row loop uses itertuples(index=False): row_context must be the
        # COLUMN values. A column literally named "Index" must keep its value; an
        # index=True mutant would overwrite "Index" with the row number (and
        # rename the real column), so `Index * 2` would compute on the row number.
        out = _run(
            {"Index": [9.0, 8.0], "b": [0.0, 0.0]},
            [
                ("Index", _col("passthrough")),
                ("b", _col("derived", provider_config=(("expression", "Index * 2"),))),
            ],
        )
        assert out["b"] == [18.0, 16.0]

    def test_eval_error_names_the_column_and_row_index(self) -> None:
        # apply_derived's error message names the column + row index for the
        # operator; the handler must forward the real column= and row_index=, not
        # None. A divide-by-zero on the second row (index 1) must surface both.
        import pytest

        # error at row 3 (index-distinctive); expression has no "3" so the row
        # index can only appear if it is forwarded (not None).
        with pytest.raises(Exception) as exc:
            _run(
                {"z": [5.0, 5.0, 5.0, 0.0], "c": [0.0, 0.0, 0.0, 0.0]},
                [
                    ("z", _col("passthrough")),
                    ("c", _col("derived", provider_config=(("expression", "9 / z"),))),
                ],
            )
        msg = str(exc.value)
        assert "'c'" in msg and "row 3" in msg  # column name + row index forwarded

    def test_supports_strategy_derived(self) -> None:
        """SCALAR_HANDLERS must advertise derived as a supported strategy."""
        adapter = PandasExecutionAdapter()
        assert adapter.supports_strategy("derived") is True

    def test_derived_null_passthrough_explicit_null_mode(self) -> None:
        """Null values in source column propagate as null in explicit_null mode."""
        import pyarrow as pa

        table = pa.table(
            {
                "a": pa.array([1.0, None, 3.0], type=pa.float64()),
                "b": pa.array([10.0, 20.0, 30.0], type=pa.float64()),
            }
        )
        plan = _plan(
            [
                (
                    "t",
                    TableSeed(
                        per_column=(
                            ("a", _col("passthrough")),
                            (
                                "b",
                                _col(
                                    "derived",
                                    provider_config=(
                                        ("expression", "a + 1"),
                                        ("null_propagation", "explicit_null"),
                                    ),
                                ),
                            ),
                        ),
                        per_group=(),
                    ),
                )
            ]
        )
        result = PandasExecutionAdapter().run_single(
            plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        out_b = result.output.column("b").to_pylist()
        assert out_b[0] == 2.0
        assert out_b[1] is None  # null propagated
        assert out_b[2] == 4.0

    def test_derived_bounds_clip_via_adapter(self) -> None:
        """Values outside [min, max] are clipped through the adapter."""
        out = _run(
            {"a": [200.0, 50.0, -5.0]},
            [
                (
                    "a",
                    _col(
                        "derived",
                        provider_config=(
                            ("expression", "a"),
                            ("bounds", {"min": 0, "max": 100}),
                        ),
                    ),
                ),
            ],
        )
        assert out["a"][0] == 100.0  # clipped to max
        assert out["a"][1] == 50.0  # unchanged
        assert out["a"][2] == 0.0  # clipped to min

    def test_derived_deterministic_same_output(self) -> None:
        """Same plan + same table -> same output (determinism through adapter)."""
        out1 = _run(
            {"a": [1.0, 2.0, 3.0], "b": [4.0, 5.0, 6.0]},
            [
                ("a", _col("passthrough")),
                ("b", _col("derived", provider_config=(("expression", "a * 2 + b"),))),
            ],
        )
        out2 = _run(
            {"a": [1.0, 2.0, 3.0], "b": [4.0, 5.0, 6.0]},
            [
                ("a", _col("passthrough")),
                ("b", _col("derived", provider_config=(("expression", "a * 2 + b"),))),
            ],
        )
        assert out1["b"] == out2["b"]

    def test_derived_mask_mode_replaces_column_with_expression_result(self) -> None:
        """Derived evaluates the expression against the source row and replaces the column.

        This drives the mask path: an existing source column is overwritten with
        the closed-grammar expression result. The generate path is covered by the
        integration tests in tests/unit/transforms/test_derived.py::TestDerivedGeneratePath.
        """
        out = _run(
            {"a": [3.0, 6.0, 9.0], "b": [1.0, 2.0, 3.0]},
            [
                ("a", _col("passthrough")),
                ("b", _col("derived", provider_config=(("expression", "a / b"),))),
            ],
        )
        # b = a / b: 3/1=3, 6/2=3, 9/3=3
        assert all(v == 3.0 for v in out["b"])

    def test_derived_slice_last4_via_adapter(self) -> None:
        """HC-6: last4(firstname) works on the MASK path via slice(firstname, -4).

        This is the gap HC-6 closes -- composite-ID expressions like last4
        now work through the shared derived/Lark evaluator on mask, not only
        generate-side.
        """
        table = pa.table(
            {
                "firstname": pa.array(["Alexander", "Bo"], type=pa.string()),
                "last4": pa.array(["X", "Y"], type=pa.string()),
            }
        )
        plan = _plan(
            [
                (
                    "t",
                    TableSeed(
                        per_column=(
                            ("firstname", _col("passthrough")),
                            (
                                "last4",
                                _col(
                                    "derived",
                                    provider_config=(("expression", "slice(firstname, -4)"),),
                                ),
                            ),
                        ),
                        per_group=(),
                    ),
                )
            ]
        )
        result = PandasExecutionAdapter().run_single(
            plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        last4 = result.output.column("last4").to_pylist()
        assert last4[0] == "nder"  # "Alexander"[-4:]
        assert last4[1] == "Bo"  # "Bo"[-4:] clamps to the whole (short) string

    def test_derived_string_expression_via_adapter(self) -> None:
        """String concat expression works through the adapter."""
        table = pa.table(
            {
                "first": pa.array(["Alice", "Bob"], type=pa.string()),
                "last": pa.array(["Smith", "Jones"], type=pa.string()),
                "full": pa.array(["X", "Y"], type=pa.string()),
            }
        )
        plan = _plan(
            [
                (
                    "t",
                    TableSeed(
                        per_column=(
                            ("first", _col("passthrough")),
                            ("last", _col("passthrough")),
                            (
                                "full",
                                _col(
                                    "derived",
                                    provider_config=(
                                        ("expression", 'concat(first, concat(" ", last))'),
                                    ),
                                ),
                            ),
                        ),
                        per_group=(),
                    ),
                )
            ]
        )
        result = PandasExecutionAdapter().run_single(
            plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        full = result.output.column("full").to_pylist()
        assert full[0] == "Alice Smith"
        assert full[1] == "Bob Jones"


# ---------------------------------------------------------------------------
# Slice 0 item 3: the expression's visible context is exactly its declared
# references, read by real column name (plan 2026-10-31-slice0-derived-visibility).
# Labels: FAIL-BEFORE (red on main), CHARACTERIZATION (green on main and after).
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from decoy_engine.execution._errors import ExecutionError  # noqa: E402
from decoy_engine.execution._transforms import TransformError  # noqa: E402


def _derived(expression: str) -> ColumnSeed:
    return _col("derived", provider_config=(("expression", expression),))


def _run_table(table: pa.Table, per_column: list[tuple[str, ColumnSeed]]) -> pa.Table:
    plan = _plan([("t", TableSeed(per_column=tuple(per_column), per_group=()))])
    return (
        PandasExecutionAdapter()
        .run_single(plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS)
        .output
    )


def _float_table(columns: dict[str, list[float]]) -> pa.Table:
    return pa.Table.from_arrays(
        [pa.array(v, pa.float64()) for v in columns.values()], names=list(columns)
    )


def test_expression_never_reads_an_undeclared_column_through_a_positional_alias() -> None:
    # T1, FAIL-BEFORE (main: [999.0, 888.0], the undeclared `first name` at position 1).
    table = _float_table(
        {"id": [1.0, 2.0], "first name": [999.0, 888.0], "_1": [5.0, 6.0], "d": [0.0, 0.0]}
    )
    out = _run_table(table, [("_1", _col("passthrough")), ("d", _derived("_1 + 0"))])
    assert out.column("d").to_pylist() == [5.0, 6.0]


def test_declared_underscore_column_is_readable() -> None:
    # T2, FAIL-BEFORE (main: uncoded per-row ValueError naming '_total').
    table = _float_table({"_total": [7.0, 8.0], "d": [0.0, 0.0]})
    out = _run_table(table, [("_total", _col("passthrough")), ("d", _derived("_total * 2"))])
    assert out.column("d").to_pylist() == [14.0, 16.0]


# T6(a): valid declared references read correctly. `_1` at position 1 coincides with its
# positional alias, so it is CHARACTERIZATION; the other underscore shapes are FAIL-BEFORE.
_T6A = [
    pytest.param(["total", "d"], "total", id="identifier-CHARACTERIZATION"),
    pytest.param(["x2", "d"], "x2", id="digit-suffix-CHARACTERIZATION"),
    pytest.param(["class_", "d"], "class_", id="keyword-suffix-CHARACTERIZATION"),
    pytest.param(["Index", "d"], "Index", id="Index-CHARACTERIZATION"),
    pytest.param(["pad", "_1", "d"], "_1", id="_1-at-position-1-CHARACTERIZATION"),
    pytest.param(["_total", "d"], "_total", id="_total-FAIL-BEFORE"),
    pytest.param(["_1", "d"], "_1", id="_1-at-position-0-FAIL-BEFORE"),
    pytest.param(["pad", "pad2", "_1", "d"], "_1", id="_1-at-position-2-FAIL-BEFORE"),
]


@pytest.mark.parametrize(("names", "ref"), _T6A)
def test_t6a_declared_reference_reads_its_own_column(names: list[str], ref: str) -> None:
    cols = {n: [0.0, 0.0] for n in names}
    cols[ref] = [3.0, 4.0]
    per_column = [(n, _col("passthrough")) for n in names if n != "d"]
    out = _run_table(_float_table(cols), [*per_column, ("d", _derived(f"{ref} * 2"))])
    assert out.column("d").to_pylist() == [6.0, 8.0]


def _t6b_out(undeclared_values: list[float]) -> list[object]:
    # Swap cell: `_2` declared at position 0, `x`, `_1` UNDECLARED at position 2.
    table = _float_table({"_2": [5.0, 6.0], "x": [1.0, 2.0], "_1": undeclared_values})
    table = table.append_column("d", pa.array([0.0, 0.0]))
    out = _run_table(
        table, [("_2", _col("passthrough")), ("x", _col("passthrough")), ("d", _derived("_2 + 0"))]
    )
    return out.column("d").to_pylist()


def test_t6b_swap_cell_never_reads_the_undeclared_column() -> None:
    # FAIL-BEFORE (main: [999.0, 888.0]).
    assert _t6b_out([999.0, 888.0]) == [5.0, 6.0]
    assert _t6b_out([-1.0, -2.0]) == [5.0, 6.0]


def test_t6b_case_a_output_ignores_undeclared_values() -> None:
    # FAIL-BEFORE (main reads `first name`).
    for undeclared in ([999.0, 888.0], [1.5, 2.5]):
        table = _float_table(
            {"id": [1.0, 2.0], "first name": undeclared, "_1": [5.0, 6.0], "d": [0.0, 0.0]}
        )
        out = _run_table(table, [("_1", _col("passthrough")), ("d", _derived("_1 + 0"))])
        assert out.column("d").to_pylist() == [5.0, 6.0]


@pytest.mark.parametrize("sibling", ["first name", "a.b", "naïve", "Index"])
def test_t6b_undeclared_sibling_without_collision_does_not_influence(sibling: str) -> None:
    # CHARACTERIZATION (main output unaffected).
    for values in ([999.0, 888.0], [1.0, 2.0]):
        table = _float_table({"total": [3.0, 4.0], sibling: values, "d": [0.0, 0.0]})
        out = _run_table(table, [("total", _col("passthrough")), ("d", _derived("total * 2"))])
        assert out.column("d").to_pylist() == [6.0, 8.0]


@pytest.mark.parametrize("expression", ["first name + 0", "a.b + 0", "naïve + 0", "__class__"])
def test_t6c_non_identifier_references_are_refused_at_parse(expression: str) -> None:
    # CHARACTERIZATION: the closed grammar refuses before any handler runs.
    from decoy_engine.errors import ValidationError
    from decoy_engine.transforms.derived import DerivedConfig

    with pytest.raises(ValidationError):
        DerivedConfig.from_dict({"expression": expression})


def _dup_table() -> pa.Table:
    return pa.Table.from_arrays(
        [pa.array([1.0, 2.0]), pa.array([3.0, 4.0]), pa.array([0.0, 0.0])],
        names=["qty", "qty", "d"],
    )


def _assert_duplicate_refusal(exc: BaseException) -> None:
    assert type(exc) is TransformError
    assert exc.code == "duplicate_source_field_names"
    assert "qty" not in str(exc) and all("qty" not in str(a) for a in exc.args)


def test_t6d1_adapter_refuses_a_duplicate_labelled_table_before_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FAIL-BEFORE (main: the handler runs through renamed aliases).
    from decoy_engine.execution._strategies._derived import DerivedStrategyHandler

    calls: list[str] = []
    real = DerivedStrategyHandler.run
    monkeypatch.setattr(
        DerivedStrategyHandler,
        "run",
        lambda self, df, column, plan, ctx: (
            calls.append(column) or real(self, df, column, plan, ctx)
        ),
    )
    with pytest.raises(Exception) as exc:
        _run_table(_dup_table(), [("qty", _col("passthrough")), ("d", _derived("qty * 2"))])
    _assert_duplicate_refusal(exc.value)
    assert calls == []


def _truncate_seed() -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="truncate",
        provider="truncate",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(("length", 1),),
        coherent_with=(),
    )


def _gated_hash_seed() -> ColumnSeed:
    from dataclasses import replace

    from tests.parity._d1a_hash_data import column_seed

    return replace(column_seed(), when="n == 1")


def _run_route(route: str, table: pa.Table, per_column: list[tuple[str, ColumnSeed]]) -> None:
    plan = _plan([("t", TableSeed(per_column=tuple(per_column), per_group=()))])
    adapter = PandasExecutionAdapter()
    kwargs = {"registry": _REG, "relationship_graph": _GRAPH, "namespace_registry": _NS}
    if route == "full":
        adapter.run(plan, {"t": table}, **kwargs)
    else:
        adapter.run_sequential(plan, lambda _name: table, config={}, **kwargs)


@pytest.mark.parametrize("route", ["full", "seq"])
@pytest.mark.parametrize("shape", ["truncate", "gated_hash"])
def test_t6d2_duplicate_guarded_field_is_refused_with_the_code(route: str, shape: str) -> None:
    # FAIL-BEFORE (main: uncoded KeyError from _guards.py:85 (truncate) or :113 (gated hash)).
    table = pa.Table.from_arrays(
        [pa.array([1, 2], pa.int64()), pa.array([3, 4], pa.int64())], names=["n", "n"]
    )
    seed = _truncate_seed() if shape == "truncate" else _gated_hash_seed()
    with pytest.raises(Exception) as exc:
        _run_route(route, table, [("n", seed)])
    assert type(exc.value) is TransformError
    assert exc.value.code == "duplicate_source_field_names"
    assert "'n'" not in str(exc.value)


def test_t6d3_direct_handler_call_refuses_a_duplicate_labelled_frame() -> None:
    # FAIL-BEFORE (main evaluates through renamed aliases).
    import pandas as pd

    from decoy_engine.execution._strategies._derived import DerivedStrategyHandler

    df = pd.DataFrame([[1.0, 3.0, 0.0], [2.0, 4.0, 0.0]], columns=["qty", "qty", "d"])
    ctx: Any = SimpleNamespace(current_table="t")
    with pytest.raises(Exception) as exc:
        DerivedStrategyHandler().run(df, "d", _derived("qty * 2"), ctx)
    _assert_duplicate_refusal(exc.value)


def test_t8_missing_declared_ref_at_runtime_is_one_coded_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FAIL-BEFORE (main: per-row uncoded ValueError, apply_derived called).
    from decoy_engine.execution._strategies import _derived as handler_mod

    calls: list[int] = []
    real = handler_mod.apply_derived
    monkeypatch.setattr(
        handler_mod,
        "apply_derived",
        lambda *a, **k: calls.append(1) or real(*a, **k),
    )
    table = _float_table({"a": [1.0, 2.0], "d": [0.0, 0.0]})
    with pytest.raises(Exception) as exc:
        _run_table(table, [("a", _col("passthrough")), ("d", _derived("a + gone"))])
    assert type(exc.value) is ExecutionError
    assert exc.value.code == "derived_ref_missing"
    assert "gone" in str(exc.value)
    assert calls == []


def test_t8_per_row_failure_is_coded_and_value_free() -> None:
    # FAIL-BEFORE (main: uncoded ValueError echoing the cell value).
    table = pa.table({"a": ["CANARY-SECRET", "2020-01-01"], "d": [0.0, 0.0]})
    with pytest.raises(Exception) as exc:
        _run_table(
            table,
            [("a", _col("passthrough")), ("d", _derived('days_between(a, "2020-01-02")'))],
        )
    assert type(exc.value) is ExecutionError
    assert exc.value.code == "derived_evaluation_failed"
    text = "derived expression evaluation failed at column 'd', row 0: an operand value is invalid"
    assert exc.value.message == text
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_t3_identifier_named_output_snapshot() -> None:
    # CHARACTERIZATION: a 5-column identifier-named table; values pinned by hand.
    table = pa.table(
        {
            "a": [1.0, 2.0, None],
            "b": [10.0, 20.0, 30.0],
            "label": ["x", "y", "z"],
            "s": [0.0, 0.0, 0.0],
            "t": ["", "", ""],
        }
    )
    out = _run_table(
        table,
        [
            ("a", _col("passthrough")),
            ("b", _col("passthrough")),
            ("label", _col("passthrough")),
            ("s", _derived("a * 2 + b")),
            ("t", _derived('concat(label, "-")')),
        ],
    )
    assert out.column("s").to_pylist() == [12.0, 24.0, None]
    assert out.column("t").to_pylist() == ["x-", "y-", "z-"]
    assert out.column_names == ["a", "b", "label", "s", "t"]


def test_constant_expression_fills_every_row() -> None:
    # CHARACTERIZATION: an expression with no column reference still yields one value per row.
    table = _float_table({"a": [1.0, 2.0, 3.0], "d": [0.0, 0.0, 0.0]})
    out = _run_table(table, [("a", _col("passthrough")), ("d", _derived("1 + 1"))])
    assert out.column("d").to_pylist() == [2.0, 2.0, 2.0]


def test_at_e2_3_null_timestamp_feeds_derived_as_a_null_cell() -> None:
    """A parquet-style null timestamp reaches the handler as NaT and must write null."""
    table = pa.table(
        {
            "a": pa.array([None, datetime(2020, 1, 1)], type=pa.timestamp("us")),
            "b": pa.array([datetime(2020, 2, 2)] * 2, type=pa.timestamp("us")),
            "c": pa.array(["x", "y"], type=pa.string()),
        }
    )
    per_column = [
        ("a", _col("passthrough")),
        ("b", _col("passthrough")),
        ("c", _col("derived", provider_config=(("expression", "case_when(a < b, 1, 0)"),))),
    ]
    plan = _plan([("t", TableSeed(per_column=tuple(per_column), per_group=()))])
    result = PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    assert result.output.column("c").to_pylist() == [None, 1]
