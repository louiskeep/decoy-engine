"""nested strategy (engine-v2 MG-3 / M2, 2026-05-31): JSONPath-targeted
masking.

Wraps a child strategy. For each cell:
  1. Parse the cell as JSON (object or array).
  2. Use jsonpath-ng to locate one or more leaf values at `target`.
  3. Collect the leaf values into a temporary Series.
  4. Run the child strategy against that Series (single batch call).
  5. Write the masked values back into the parsed JSON at the same
     paths.
  6. Re-serialize the cell.

Single-pass collection + batch delegation preserves the child
strategy's vectorized behavior. JSON-malformed cells emit a
QualityWarning and pass through unchanged. Cells with no match for
the target path are left as-is (no warning -- a sparse path is a
valid use case).

Config (`provider_config`):
    target          str             JSONPath expression locating the
                                    leaves to mask. Required.
    strategy        str             Child strategy name (must be a key
                                    of SCALAR_HANDLERS and NOT
                                    "nested"). Required.
    strategy_config dict            Provider config for the child
                                    strategy. Optional, defaults to
                                    empty.

Established methodology citation: jsonpath-ng is the maintained
Python implementation of the JSONPath specification (RFC 9535 draft
+ Stefan Goessner's reference syntax). MIT-licensed; matches our
posture for numexpr / polars (we don't reinvent parsers).
"""

from __future__ import annotations

import json
from typing import Any

import jsonpath_ng
import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class NestedStrategyHandler:
    """JSONPath-targeted child-strategy wrapper."""

    name: str = "nested"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        # Lazy import keeps the SCALAR_HANDLERS init cycle clean (the
        # nested strategy reads from SCALAR_HANDLERS, and the
        # SCALAR_HANDLERS dict imports nested strategy at module load).
        from decoy_engine.execution._strategies import SCALAR_HANDLERS

        cfg = provider_config_to_dict(plan.provider_config)
        target_path = cfg.get("target")
        child_strategy_name = cfg.get("strategy")
        child_strategy_config = cfg.get("strategy_config") or {}

        if not isinstance(target_path, str) or not target_path:
            return df, [
                QualityWarning(
                    code="nested_target_unset",
                    provider="nested",
                    column=column,
                )
            ]
        if not isinstance(child_strategy_name, str) or not child_strategy_name:
            return df, [
                QualityWarning(
                    code="nested_strategy_unset",
                    provider="nested",
                    column=column,
                )
            ]

        if child_strategy_name == "nested":
            return df, [
                QualityWarning(
                    code="nested_recursive_nested_rejected",
                    provider="nested",
                    column=column,
                )
            ]

        child_handler = SCALAR_HANDLERS.get(child_strategy_name)
        if child_handler is None:
            return df, [
                QualityWarning(
                    code="nested_child_strategy_unknown",
                    provider="nested",
                    column=column,
                    detail={"child_strategy": child_strategy_name},
                )
            ]

        try:
            jsonpath_expr = jsonpath_ng.parse(target_path)
        except Exception as exc:
            return df, [
                QualityWarning(
                    code="nested_jsonpath_parse_error",
                    provider="nested",
                    column=column,
                    detail={"target": target_path, "error": str(exc)},
                )
            ]

        col = df[column]
        if pd.api.types.is_extension_array_dtype(col.dtype):
            col = col.astype(object)
        else:
            col = col.copy()

        warnings: list[QualityWarning] = []
        leaf_values: list[Any] = []
        # Per-row: (parsed_object, list[jsonpath_match])
        per_row_state: dict[Any, tuple[Any, list]] = {}

        for row_idx in col.index:
            cell = col.at[row_idx]
            if pd.isna(cell):
                continue
            try:
                parsed = json.loads(cell) if isinstance(cell, str) else cell
            except Exception:
                warnings.append(
                    QualityWarning(
                        code="nested_cell_json_parse_error",
                        provider="nested",
                        column=column,
                        detail={"row_idx": str(row_idx)},
                    )
                )
                continue
            matches = jsonpath_expr.find(parsed)
            if not matches:
                continue
            per_row_state[row_idx] = (parsed, list(matches))
            for m in matches:
                leaf_values.append(m.value)

        if not leaf_values:
            return df, warnings

        # Build a synthetic child seed inheriting parent fields but
        # carrying the child strategy + config.
        child_provider_config: tuple[tuple[str, Any], ...]
        if isinstance(child_strategy_config, dict):
            child_provider_config = tuple(sorted(child_strategy_config.items()))
        else:
            child_provider_config = ()
        child_seed = ColumnSeed(
            namespace=plan.namespace,
            strategy=child_strategy_name,
            provider=plan.provider,
            backend_type=plan.backend_type,
            backend_version=plan.backend_version,
            cardinality_mode=plan.cardinality_mode,
            deterministic=plan.deterministic,
            provider_config=child_provider_config,
            coherent_with=plan.coherent_with,
            technique_class=plan.technique_class,
            when=None,
        )

        # Run the child handler on the collected leaves in one batch.
        temp_col = "_nested_leaves"
        temp_df = pd.DataFrame({temp_col: leaf_values})
        temp_df, child_warnings = child_handler.run(
            temp_df, temp_col, child_seed, ctx
        )
        warnings.extend(child_warnings)
        new_leaf_values = temp_df[temp_col].tolist()

        # Writeback: walk matches in the same order leaves were
        # collected, replace each, re-serialize the cell.
        cursor = 0
        for row_idx, (parsed, matches) in per_row_state.items():
            for m in matches:
                new_value = new_leaf_values[cursor]
                cursor += 1
                m.full_path.update(parsed, new_value)
            col.at[row_idx] = json.dumps(parsed)

        df[column] = col
        return df, warnings
