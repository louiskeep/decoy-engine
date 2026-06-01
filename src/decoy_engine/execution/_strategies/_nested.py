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

Round-trip note (QA-3 F8, 2026-05-31): the cell is parsed via
`json.loads` and re-serialized via `json.dumps`. JSON serialization
canonicalizes whitespace, escapes, and key ordering, so the masked
cell is not byte-for-byte identical to the input even when the
target leaf is unchanged. Operators relying on byte-stable input
should mask upstream of any JSON normalization.

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
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._technique_class import technique_class_for
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

        # QA-3 F12 (2026-05-31, security): config errors below promote to
        # StrategyError so the runner fails the job. Pre-fix they returned
        # df UNCHANGED with a QualityWarning -- a misconfigured target
        # (e.g. `$.patient_name` typoed as `$.patientName`, or a child
        # strategy name typo) silently passed PII through. The warning
        # surfaced only in the Storm report, not at job failure, and
        # operators who don't audit Storm output had no signal.
        # Sparse-path passthrough (some cells have matches, some don't)
        # stays a non-error case per spec; that branch is handled below
        # by the "if not leaf_values" early-return without raising.
        if not isinstance(target_path, str) or not target_path:
            raise StrategyError(
                code="nested_target_unset",
                strategy="nested",
                message=(
                    f"nested target is required and must be a non-empty string "
                    f"(column={column!r}). Pre-fix this returned the column "
                    "unchanged with a QualityWarning; misconfigured targets "
                    "silently passed PII through."
                ),
            )
        if not isinstance(child_strategy_name, str) or not child_strategy_name:
            raise StrategyError(
                code="nested_strategy_unset",
                strategy="nested",
                message=(
                    f"nested child strategy is required and must be a non-empty "
                    f"string (column={column!r})."
                ),
            )

        if child_strategy_name == "nested":
            raise StrategyError(
                code="nested_recursive_nested_rejected",
                strategy="nested",
                message=(
                    f"nested cannot wrap itself recursively (column={column!r})."
                ),
            )

        child_handler = SCALAR_HANDLERS.get(child_strategy_name)
        if child_handler is None:
            raise StrategyError(
                code="nested_child_strategy_unknown",
                strategy="nested",
                message=(
                    f"nested child strategy {child_strategy_name!r} is not a "
                    f"registered SCALAR_HANDLERS key (column={column!r}). "
                    "A typo here silently dropped PII pre-fix."
                ),
            )

        try:
            jsonpath_expr = jsonpath_ng.parse(target_path)
        except Exception as exc:
            raise StrategyError(
                code="nested_jsonpath_parse_error",
                strategy="nested",
                message=(
                    f"nested target {target_path!r} is not a valid JSONPath "
                    f"expression (column={column!r}): {exc}"
                ),
            ) from exc

        col = df[column]
        if pd.api.types.is_extension_array_dtype(col.dtype):
            col = col.astype(object)
        else:
            col = col.copy()

        # QA-3 F2 (2026-05-31): positional enumeration replaces the
        # index-keyed dict. Pre-fix, this strategy iterated `col.index`
        # and keyed per_row_state on `row_idx`. On a DataFrame with
        # duplicate indexes (which is legal pandas and can arise from
        # `pd.concat` without reset_index), `col.at[row_idx]` returns a
        # SERIES of all matching rows -- not a single cell -- breaking
        # the parse step. And a dict keyed on row_idx only retains ONE
        # state entry per duplicate, silently dropping the others.
        # Positional iteration over `col.iloc[i]` removes both failure
        # modes: each row is visited exactly once, and the cursor /
        # writeback is position-indexed.
        col_values = col.to_list()

        warnings: list[QualityWarning] = []
        leaf_values: list[Any] = []
        # Per-position: (parsed_object, list[jsonpath_match]). Position
        # is the row's 0-indexed offset in the column, not its label.
        per_position_state: dict[int, tuple[Any, list]] = {}

        for pos, cell in enumerate(col_values):
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
                        detail={"row_pos": str(pos)},
                    )
                )
                continue
            matches = jsonpath_expr.find(parsed)
            if not matches:
                continue
            per_position_state[pos] = (parsed, list(matches))
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
        # QA-3 F7 (2026-05-31): resolve the CHILD's technique class. The
        # parent's `technique_class` is None for nested (intentionally;
        # see _technique_class.py), so inheriting it would always leave
        # the child seed unclassified. The child should report the
        # class of the strategy it actually runs (e.g. redact ->
        # anonymisation).
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
            technique_class=technique_class_for(child_strategy_name),
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
        # collected, replace each, re-serialize the cell. Positional
        # writeback (QA-3 F2) means we reassemble the column from
        # `col_values` rather than mutating by index label.
        cursor = 0
        for pos, (parsed, matches) in per_position_state.items():
            for m in matches:
                new_value = new_leaf_values[cursor]
                cursor += 1
                m.full_path.update(parsed, new_value)
            col_values[pos] = json.dumps(parsed)

        df[column] = pd.Series(col_values, index=df.index, dtype=object)
        return df, warnings
