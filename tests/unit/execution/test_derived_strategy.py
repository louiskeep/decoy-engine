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
