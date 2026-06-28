"""derived_aggregate column-level strategy (SP-10b / P5.S.derived_aggregate).

Computes an aggregate (sum / mean / min / max / count) over a named source
column and fills every row of the target column with the resulting scalar.
This is the intra-table variant: source and target are in the same table.
The FK-driven cross-table variant is deferred to SP-10c.

Pattern: SQL aggregate functions (SUM/AVG/MIN/MAX/COUNT) with pandas Series
(ISO/IEC 9075-1; pandas 2.x Apache-2.0). See: https://pandas.pydata.org/docs

Methodology:
  Aggregate semantics follow the SQL standard (ISO/IEC 9075-1, General
  Concepts, clause on set functions). Null handling follows the SQL rule:
  NULLs are excluded from SUM / AVG / MIN / MAX; COUNT counts non-null
  rows only. pandas Series.sum / .mean / .min / .max / .count with default
  skipna=True implements this exactly.

Security design:
  The operator set is a CLOSED enumeration (AGGREGATE_OPS). Only the five
  listed op names can construct a valid DerivedAggregateConfig.  Any other
  value raises PlanCompileError at config-parse time, before any DataFrame
  is touched. There is no eval() or exec() in this module.

Determinism:
  Same input column -> same scalar aggregate on every run. No RNG involved.
  The strategy is fully deterministic.

No raw-value leakage:
  The output is a scalar aggregate (e.g. sum, mean), never an individual
  row value. The target column carries the same scalar in every row.

Validation timing:
  op + column: config-parse time (DerivedAggregateConfig.from_dict).
  column-ref existence: plan-compile time (check_derived_aggregate_refs
  in plan/_checks.py, called in compile_plan + run_config_only_checks).
  Validation never mutates (per engine rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from decoy_engine.plan._errors import PlanCompileError

# Type alias for the already-generated column snapshot passed through the
# synthesize layer. Keys are column names; values are the generated value lists.
_GeneratedSnapshot = dict[str, list[Any]]

# Closed enumeration of allowed aggregate operations.
# Any op name outside this set raises PlanCompileError at config-parse time.
AGGREGATE_OPS: frozenset[str] = frozenset({"sum", "mean", "min", "max", "count"})


@dataclass(frozen=True)
class DerivedAggregateConfig:
    """Configuration for a derived_aggregate column.

    Attributes:
        op:     The aggregate function to apply. One of sum / mean / min /
                max / count.
        column: Name of the source column in the same table to aggregate.
                Must exist at plan-compile time (check_derived_aggregate_refs).
    """

    op: str
    column: str

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> DerivedAggregateConfig:
        """Parse and validate a derived_aggregate config dict.

        op and column are validated at parse time. Column-ref existence is
        validated at plan-compile time via check_derived_aggregate_refs.

        Args:
            cfg: Config dict with required keys ``op`` and ``column``.

        Raises:
            PlanCompileError: ``op`` is missing or not in AGGREGATE_OPS.
            PlanCompileError: ``column`` is missing or empty.
        """
        op = cfg.get("op")
        if not op:
            raise PlanCompileError(
                code="derived_aggregate_op_missing",
                path="provider_config.op",
                message=(
                    "'op' is required for the derived_aggregate strategy. "
                    f"Allowed values: {sorted(AGGREGATE_OPS)!r}."
                ),
            )
        op = str(op)
        if op not in AGGREGATE_OPS:
            raise PlanCompileError(
                code="derived_aggregate_op_invalid",
                path="provider_config.op",
                message=(f"'op' must be one of {sorted(AGGREGATE_OPS)!r}; got {op!r}."),
            )

        column = cfg.get("column")
        if not column:
            raise PlanCompileError(
                code="derived_aggregate_column_missing",
                path="provider_config.column",
                message=(
                    "'column' is required for the derived_aggregate strategy. "
                    "Provide the name of the source column to aggregate "
                    "(must be in the same table)."
                ),
            )

        return cls(op=op, column=str(column))


def generate_derived_aggregate_column(
    col: dict[str, Any],
    n: int,
    generated: _GeneratedSnapshot,
) -> list[Any]:
    """Compute an aggregate over an already-generated sibling and fill n rows.

    Pattern: SQL aggregate functions (SUM/AVG/MIN/MAX/COUNT) with pandas Series
    (ISO/IEC 9075-1; pandas 2.x Apache-2.0). See: https://pandas.pydata.org/docs

    Called from ``generation.synthesize._generate_column`` for generate-kind
    ``derived_aggregate`` columns. Reads the source column values from
    ``generated`` (sibling columns declared before this one) and returns a
    list of ``n`` copies of the aggregate scalar.

    Args:
        col:       Column config dict with ``op`` and ``column`` keys.
        n:         Number of rows to generate.
        generated: Mapping of already-generated sibling column values.

    Returns:
        A list of ``n`` copies of the aggregate scalar.
    """
    config = DerivedAggregateConfig.from_dict(
        {"op": col.get("op", ""), "column": col.get("column", "")}
    )
    source_values = generated.get(config.column, [])
    series = pd.Series(source_values, dtype=object)
    scalar = apply_derived_aggregate(config, series)
    return [scalar] * n


def apply_derived_aggregate(config: DerivedAggregateConfig, series: pd.Series) -> Any:
    """Compute the aggregate scalar from *series* per config.op.

    Pattern: SQL aggregate functions (ISO/IEC 9075-1) via pandas Series
    (pandas 2.x, Apache-2.0; https://pandas.pydata.org/docs).

    NULLs are excluded from all aggregates (skipna=True, matching the SQL
    rule that NULLs are ignored by set functions). count returns an int;
    sum / mean / min / max return float or the native dtype of the series.

    Args:
        config: Parsed DerivedAggregateConfig with the op and source column.
        series: The source column as a pandas Series.

    Returns:
        The aggregate scalar (float, int, or native dtype).
    """
    op = config.op
    if op == "sum":
        return series.sum(skipna=True)
    if op == "mean":
        return series.mean(skipna=True)
    if op == "min":
        return series.min(skipna=True)
    if op == "max":
        return series.max(skipna=True)
    # op == "count": count non-null values (equivalent to SQL COUNT(col))
    return int(series.count())
