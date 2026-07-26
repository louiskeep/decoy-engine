"""Unit tests for derived_aggregate strategy (SP-10b TDD).

Written BEFORE the implementation per the TDD contract (testing.md).

derived_aggregate is a separate masking/generation strategy that computes
an aggregate (sum / mean / min / max / count) over a source column and
fills every row of the target column with the scalar result.

Config:
    op:     Required. One of sum / mean / min / max / count.
    column: Required. Source column name in the same table to aggregate.

Methodology:
    SQL aggregate functions (SUM, AVG, MIN, MAX, COUNT). Semantics follow
    pandas Series.sum / .mean / .min / .max / .count (pandas 2.x, Apache-2.0;
    https://pandas.pydata.org/docs). Deterministic: same input series ->
    same aggregate scalar. No raw-value leakage: the output is always a
    single aggregate, never an individual row value.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.plan._errors import PlanCompileError


class TestDerivedAggregateConfig:
    """DerivedAggregateConfig.from_dict validates op + column at parse time."""

    def test_valid_sum_config_accepted(self) -> None:
        from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

        cfg = DerivedAggregateConfig.from_dict({"op": "sum", "column": "amount"})
        assert cfg.op == "sum"
        assert cfg.column == "amount"

    def test_valid_mean_config_accepted(self) -> None:
        from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

        cfg = DerivedAggregateConfig.from_dict({"op": "mean", "column": "amount"})
        assert cfg.op == "mean"

    def test_valid_min_config_accepted(self) -> None:
        from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

        DerivedAggregateConfig.from_dict({"op": "min", "column": "val"})

    def test_valid_max_config_accepted(self) -> None:
        from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

        DerivedAggregateConfig.from_dict({"op": "max", "column": "val"})

    def test_valid_count_config_accepted(self) -> None:
        from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

        cfg = DerivedAggregateConfig.from_dict({"op": "count", "column": "val"})
        assert cfg.op == "count"

    def test_missing_op_raises_plan_compile_error(self) -> None:
        from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

        with pytest.raises(PlanCompileError, match="op"):
            DerivedAggregateConfig.from_dict({"column": "amount"})

    def test_missing_column_raises_plan_compile_error(self) -> None:
        from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

        with pytest.raises(PlanCompileError, match="column"):
            DerivedAggregateConfig.from_dict({"op": "sum"})

    def test_invalid_op_raises_plan_compile_error(self) -> None:
        from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

        with pytest.raises(PlanCompileError, match="op"):
            DerivedAggregateConfig.from_dict({"op": "median", "column": "amount"})

    def test_empty_config_raises(self) -> None:
        from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

        with pytest.raises(PlanCompileError):
            DerivedAggregateConfig.from_dict({})


class TestApplyDerivedAggregate:
    """apply_derived_aggregate computes the correct scalar per op."""

    def _make_series(self, values):
        import pandas as pd

        return pd.Series(values, dtype=float)

    def _apply(self, op: str, values):
        from decoy_engine.transforms.derived_aggregate import (
            DerivedAggregateConfig,
            apply_derived_aggregate,
        )

        cfg = DerivedAggregateConfig.from_dict({"op": op, "column": "x"})
        return apply_derived_aggregate(cfg, self._make_series(values))

    def test_sum(self) -> None:
        assert self._apply("sum", [1.0, 2.0, 3.0]) == 6.0

    def test_mean(self) -> None:
        result = self._apply("mean", [1.0, 2.0, 3.0])
        assert abs(result - 2.0) < 1e-9

    def test_min(self) -> None:
        assert self._apply("min", [3.0, 1.0, 2.0]) == 1.0

    def test_max(self) -> None:
        assert self._apply("max", [3.0, 1.0, 2.0]) == 3.0

    def test_min_excludes_nulls(self) -> None:
        # min uses skipna=True (SQL null-exclusion); a skipna=False mutant would
        # return NaN once any null is present.
        assert self._apply("min", [3.0, None, 1.0, 2.0]) == 1.0

    def test_max_excludes_nulls(self) -> None:
        # max uses skipna=True; a skipna=False mutant would return NaN.
        assert self._apply("max", [3.0, None, 1.0, 2.0]) == 3.0

    def test_count(self) -> None:
        assert self._apply("count", [1.0, None, 3.0]) == 2

    def test_deterministic_same_input_same_output(self) -> None:
        import pandas as pd

        from decoy_engine.transforms.derived_aggregate import (
            DerivedAggregateConfig,
            apply_derived_aggregate,
        )

        cfg = DerivedAggregateConfig.from_dict({"op": "sum", "column": "x"})
        s = pd.Series([1.0, 2.0, 3.0])
        r1 = apply_derived_aggregate(cfg, s)
        r2 = apply_derived_aggregate(cfg, s)
        assert r1 == r2

    def test_null_values_excluded_from_sum(self) -> None:
        """Nulls are excluded per pandas default (skipna=True)."""
        result = self._apply("sum", [1.0, None, 3.0])
        assert abs(result - 4.0) < 1e-9

    def test_null_values_excluded_from_mean(self) -> None:
        result = self._apply("mean", [2.0, None, 4.0])
        assert abs(result - 3.0) < 1e-9


class TestDerivedAggregateStrategyRegistration:
    """Strategy is registered in SCALAR_HANDLERS and supports_strategy returns True."""

    def test_derived_aggregate_in_scalar_handlers(self) -> None:
        from decoy_engine.execution._strategies import SCALAR_HANDLERS

        assert "derived_aggregate" in SCALAR_HANDLERS

    def test_adapter_supports_derived_aggregate(self) -> None:
        from decoy_engine.execution import PandasExecutionAdapter

        assert PandasExecutionAdapter().supports_strategy("derived_aggregate") is True


class TestDerivedAggregateThroughRealRunPath:
    """Integration: derived_aggregate through PandasExecutionAdapter."""

    def _run(self, columns: dict, per_column: list) -> dict:
        from types import SimpleNamespace

        from decoy_engine.execution import PandasExecutionAdapter
        from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        reg = get_default_registry()
        graph = RelationshipGraph(edges=(), ordering=())
        ns = NamespaceRegistry(bindings=())
        seed = b"\xda\xda" * 4

        arrays = {}
        for col, vals in columns.items():
            if vals and all(v is None or isinstance(v, (int, float)) for v in vals):
                arrays[col] = pa.array(vals, type=pa.float64())
            else:
                arrays[col] = pa.array(vals, type=pa.string())
        table = pa.table(arrays)

        def _col(strategy: str, **kw):
            return ColumnSeed(
                namespace=None,
                strategy=strategy,
                provider="x_nobackend",
                backend_type="faker",
                backend_version="v",
                cardinality_mode="reuse",
                deterministic=False,
                provider_config=kw.get("provider_config", ()),
                coherent_with=(),
            )

        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=seed,
                per_table=(
                    (
                        "t",
                        TableSeed(per_column=tuple(per_column), per_group=()),
                    ),
                ),
            )
        )
        result = PandasExecutionAdapter().run_single(
            plan, table, registry=reg, relationship_graph=graph, namespace_registry=ns
        )
        return {c: result.output.column(c).to_pylist() for c in columns}

    def _col(self, strategy: str, **kw):
        from decoy_engine.plan._types import ColumnSeed

        return ColumnSeed(
            namespace=None,
            strategy=strategy,
            provider="x_nobackend",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=kw.get("provider_config", ()),
            coherent_with=(),
        )

    def test_sum_fills_all_rows_with_total(self) -> None:
        """derived_aggregate sum fills every row of the target column with the total."""
        out = self._run(
            {"amount": [10.0, 20.0, 30.0], "total": [0.0, 0.0, 0.0]},
            [
                ("amount", self._col("passthrough")),
                (
                    "total",
                    self._col(
                        "derived_aggregate",
                        provider_config=(("op", "sum"), ("column", "amount")),
                    ),
                ),
            ],
        )
        # All rows should equal sum(amount) = 60
        assert out["total"] == [60.0, 60.0, 60.0]

    def test_mean_fills_all_rows_with_average(self) -> None:
        out = self._run(
            {"val": [10.0, 20.0, 30.0], "avg": [0.0, 0.0, 0.0]},
            [
                ("val", self._col("passthrough")),
                (
                    "avg",
                    self._col(
                        "derived_aggregate",
                        provider_config=(("op", "mean"), ("column", "val")),
                    ),
                ),
            ],
        )
        assert all(abs(v - 20.0) < 1e-9 for v in out["avg"])

    def test_max_fills_all_rows_with_max_value(self) -> None:
        out = self._run(
            {"score": [50.0, 90.0, 70.0], "top": [0.0, 0.0, 0.0]},
            [
                ("score", self._col("passthrough")),
                (
                    "top",
                    self._col(
                        "derived_aggregate",
                        provider_config=(("op", "max"), ("column", "score")),
                    ),
                ),
            ],
        )
        assert out["top"] == [90.0, 90.0, 90.0]

    def test_min_fills_all_rows_with_min_value(self) -> None:
        out = self._run(
            {"score": [50.0, 90.0, 70.0], "low": [0.0, 0.0, 0.0]},
            [
                ("score", self._col("passthrough")),
                (
                    "low",
                    self._col(
                        "derived_aggregate",
                        provider_config=(("op", "min"), ("column", "score")),
                    ),
                ),
            ],
        )
        assert out["low"] == [50.0, 50.0, 50.0]

    def test_count_fills_all_rows_with_non_null_count(self) -> None:
        table = pa.table(
            {
                "x": pa.array([1.0, None, 3.0], type=pa.float64()),
                "cnt": pa.array([0.0, 0.0, 0.0], type=pa.float64()),
            }
        )
        from types import SimpleNamespace

        from decoy_engine.execution import PandasExecutionAdapter
        from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        reg = get_default_registry()
        graph = RelationshipGraph(edges=(), ordering=())
        ns = NamespaceRegistry(bindings=())
        seed = b"\xda\xda" * 4

        col = ColumnSeed(
            namespace=None,
            strategy="derived_aggregate",
            provider="x_nobackend",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(("op", "count"), ("column", "x")),
            coherent_with=(),
        )
        pass_col = ColumnSeed(
            namespace=None,
            strategy="passthrough",
            provider="x_nobackend",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(),
            coherent_with=(),
        )
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=seed,
                per_table=(
                    ("t", TableSeed(per_column=(("x", pass_col), ("cnt", col)), per_group=())),
                ),
            )
        )
        result = PandasExecutionAdapter().run_single(
            plan, table, registry=reg, relationship_graph=graph, namespace_registry=ns
        )
        cnt_vals = result.output.column("cnt").to_pylist()
        # count of non-null x values is 2
        assert cnt_vals == [2, 2, 2]

    def test_invalid_op_raises_at_config_parse(self) -> None:
        """An invalid op raises PlanCompileError via the strategy handler (fail-closed)."""
        from decoy_engine.transforms.derived_aggregate import DerivedAggregateConfig

        with pytest.raises(PlanCompileError, match="op"):
            DerivedAggregateConfig.from_dict({"op": "product", "column": "x"})


class TestDerivedAggregateGeneratePath:
    """Integration: derived_aggregate in a generate table."""

    def test_derived_aggregate_sum_in_generate_table(self) -> None:
        """sum in generate mode aggregates an already-generated sibling column."""
        from tests.unit._dps_helpers import compile_and_generate

        cfg = {
            "version": 1,
            "global_settings": {"seed": 0},
            "sources": {},
            "tables": [
                {
                    "name": "t",
                    "row_count": 3,
                    "generate_columns": [
                        {
                            "name": "amount",
                            "type": "categorical",
                            "categories": [10],
                            "weights": [1],
                        },
                        {
                            "name": "total",
                            "type": "derived_aggregate",
                            "op": "sum",
                            "column": "amount",
                        },
                    ],
                }
            ],
            "targets": {"t": {"type": "file", "format": "csv", "path": "out.csv"}},
        }
        tbl = compile_and_generate(cfg)["t"]
        totals = tbl.column("total").to_pylist()
        amounts = tbl.column("amount").to_pylist()
        expected_sum = sum(amounts)
        # Every row of total should equal sum(amount)
        assert all(t == expected_sum for t in totals)

    def test_derived_aggregate_mean_in_generate_table(self) -> None:
        from tests.unit._dps_helpers import compile_and_generate

        cfg = {
            "version": 1,
            "global_settings": {"seed": 0},
            "sources": {},
            "tables": [
                {
                    "name": "t",
                    "row_count": 4,
                    "generate_columns": [
                        {
                            "name": "val",
                            "type": "categorical",
                            "categories": [8],
                            "weights": [1],
                        },
                        {
                            "name": "avg_val",
                            "type": "derived_aggregate",
                            "op": "mean",
                            "column": "val",
                        },
                    ],
                }
            ],
            "targets": {"t": {"type": "file", "format": "csv", "path": "out.csv"}},
        }
        tbl = compile_and_generate(cfg)["t"]
        avg_vals = tbl.column("avg_val").to_pylist()
        # All values are 8, so mean is 8.0
        assert all(abs(v - 8.0) < 1e-9 for v in avg_vals)


class TestDerivedAggregateMetadata:
    """Technique class and distribution behavior are correctly registered."""

    def test_technique_class_is_pseudonymisation(self) -> None:
        from decoy_engine.execution._technique_class import technique_class_for

        # Conservative default: min/max could expose exact source values.
        assert technique_class_for("derived_aggregate") == "pseudonymisation"

    def test_distribution_behavior_is_coarsens(self) -> None:
        from decoy_engine.execution._distribution_behavior import distribution_behavior_for

        assert distribution_behavior_for("derived_aggregate") == "coarsens"
