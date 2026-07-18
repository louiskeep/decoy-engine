"""derived column-level strategy (SP-10 / P5.S.derived).

Computes a column's value from other columns in the same row via a
closed-vocabulary expression (Lark parser from SP-06 / P5.INFRA.2).

Pattern: Lark EBNF closed-grammar expression evaluation
(lark-parser/lark, MIT). See: https://github.com/lark-parser/lark

Security design:
  Expression compilation (compile_expr) runs once at config-parse time via
  DerivedConfig.from_dict. The closed grammar in grammar.lark is the sole
  security boundary: only the explicit operator set can parse; any construct
  outside it (function calls, attribute access, import, dunder identifiers)
  raises ValidationError before any row data is touched. There is NO Python
  eval(), exec(), or __import__ anywhere in this path.

Validation timing:
  - Expression syntax: config-parse time (DerivedConfig.from_dict via compile_expr).
  - Column-ref existence and cyclic references: PLAN-COMPILE time via
    check_derived_column_refs in plan/_checks.py, which wires into compile_plan
    and run_config_only_checks. Validation never mutates (per engine rule).

Null propagation modes:
  explicit_null  Output is None when any referenced column value is None or NaN.
  sentinel       None/NaN column values are replaced with "" before evaluation;
                 the expression always runs.
  default        None/NaN column values are coerced to 0; the expression always
                 runs.

Bounds:
  {min: float, max: float} clips numeric output after evaluation.
  Non-numeric results (strings, booleans, None) are passed through unchanged;
  bounds apply only when the expression evaluates to a real number (int or float
  that is not bool).

Mask mode and gen mode both evaluate the same closed expression against the
row context. Determinism is inherent: same row context -> same output, no RNG
involved. There is no code branching between mask and gen.

In mask mode the row context is built from the source table's existing column
values. In generate mode the row context is built from already-generated sibling
columns (declared-order sequential semantics, same as the statistical/condition_on
pattern). Both modes call apply_derived with a complete row context dict.

Error handling:
  Per-row evaluation errors (e.g. ZeroDivisionError, TypeError) are wrapped in
  ValueError and re-raised with the column name and row index for diagnosability.
  Callers pass these via the column= and row_index= keyword arguments.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from decoy_engine.expressions import CompiledExpression, compile_expr, evaluate
from decoy_engine.plan._errors import PlanCompileError

# Valid null_propagation values.
_NULL_PROPAGATION_MODES = frozenset({"explicit_null", "sentinel", "default"})

# Sentinel coercion value for sentinel mode.
_SENTINEL_VALUE = ""

# Default coercion value for default mode.
_DEFAULT_VALUE = 0


@dataclass(frozen=True)
class DerivedConfig:
    """Configuration for a derived column expression.

    Attributes:
        expression: The original expression string as supplied in config.
        compiled: The pre-compiled Lark expression (compiled once at parse time).
        bounds: Optional {min, max} dict to clip numeric output. None means
            no clipping.
        null_propagation: How null/NaN input column values are handled.
            "explicit_null" (default): output is None if any referenced column
            is None/NaN.
            "sentinel": None/NaN is replaced with "" before evaluation.
            "default": None/NaN is coerced to 0 before evaluation.
    """

    expression: str
    compiled: CompiledExpression
    bounds: dict[str, float] | None
    null_propagation: str

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> DerivedConfig:
        """Parse and validate a derived config dict.

        Expression syntax is validated here via compile_expr (closed-grammar
        compile time). Column-ref existence and cycle detection are validated
        at plan-compile time via check_derived_column_refs in plan/_checks.py.

        Args:
            cfg: Config dict with required key ``expression`` and optional
                ``bounds`` and ``null_propagation``.

        Raises:
            PlanCompileError: ``expression`` is missing.
            ValidationError: ``expression`` is outside the closed grammar.
            PlanCompileError: ``null_propagation`` value is not recognised.
        """
        expr = cfg.get("expression")
        if not expr:
            raise PlanCompileError(
                code="derived_expression_missing",
                path="provider_config.expression",
                message=(
                    "'expression' is required for the derived strategy. "
                    "Provide a closed-vocabulary expression using column "
                    "references and the supported operators (+, -, *, /, //, "
                    "==, !=, <, >, <=, >=, in, and, or, not, concat() (n-arg), "
                    "slice(s, start[, end]), days_between(), if/else ternary, "
                    "literals)."
                ),
            )

        # compile_expr raises ValidationError on any expression outside the closed grammar.
        compiled = compile_expr(str(expr))

        bounds_raw = cfg.get("bounds")
        bounds: dict[str, float] | None = None
        if bounds_raw is not None:
            if not isinstance(bounds_raw, dict):
                raise PlanCompileError(
                    code="derived_bounds_invalid",
                    path="provider_config.bounds",
                    message=(
                        f"'bounds' must be a dict with 'min' and/or 'max' keys, "
                        f"got {type(bounds_raw).__name__!r}."
                    ),
                )
            bounds = {}
            for key in ("min", "max"):
                val = bounds_raw.get(key)
                if val is not None:
                    bounds[key] = float(val)
            if "min" in bounds and "max" in bounds and bounds["min"] > bounds["max"]:
                raise PlanCompileError(
                    code="derived_bounds_inverted",
                    path="provider_config.bounds",
                    message=(
                        f"'bounds.min' ({bounds['min']}) must not exceed "
                        f"'bounds.max' ({bounds['max']}). "
                        f"Swap the values or remove one of them."
                    ),
                )

        null_propagation = str(cfg.get("null_propagation", "explicit_null"))
        if null_propagation not in _NULL_PROPAGATION_MODES:
            raise PlanCompileError(
                code="derived_null_propagation_invalid",
                path="provider_config.null_propagation",
                message=(
                    f"'null_propagation' must be one of "
                    f"{sorted(_NULL_PROPAGATION_MODES)!r}, "
                    f"got {null_propagation!r}."
                ),
            )

        return cls(
            expression=str(expr),
            compiled=compiled,
            bounds=bounds,
            null_propagation=null_propagation,
        )


def _get_column_refs(compiled: CompiledExpression) -> frozenset[str]:
    """Extract the set of column names referenced in a compiled expression.

    Walks the Lark parse tree and collects all ``col_ref`` node identifiers.
    Called by check_derived_column_refs (plan-compile time) to validate that
    referenced columns exist in the same table and that no cycles exist.

    Args:
        compiled: A pre-compiled expression from compile_expr.

    Returns:
        A frozenset of column name strings.
    """
    refs: set[str] = set()
    for subtree in compiled.tree.iter_subtrees():
        if subtree.data == "col_ref":
            refs.add(str(subtree.children[0]))
    return frozenset(refs)


def _is_null(value: Any) -> bool:
    """Return True when *value* is None or a float NaN."""
    if value is None:
        return True
    return isinstance(value, float) and math.isnan(value)


def _coerce_context(
    row_context: dict[str, Any],
    col_refs: frozenset[str],
    null_propagation: str,
) -> dict[str, Any] | None:
    """Prepare the row context per the null_propagation mode.

    Returns:
        A (possibly coerced) context dict, or None when explicit_null
        mode has detected a null/NaN in any referenced column (signalling
        that the output should be None).
    """
    if null_propagation == "explicit_null":
        for ref in col_refs:
            if ref in row_context and _is_null(row_context[ref]):
                return None
        return row_context

    coerced: dict[str, Any] = dict(row_context)
    replacement = _SENTINEL_VALUE if null_propagation == "sentinel" else _DEFAULT_VALUE
    for ref in col_refs:
        if ref in coerced and _is_null(coerced[ref]):
            coerced[ref] = replacement
    return coerced


def _apply_bounds(value: Any, bounds: dict[str, float] | None) -> Any:
    """Clip *value* to [min, max] when bounds are configured.

    Only applies to int or float values that are not bool (booleans are a
    subtype of int in Python but are not numeric in the masking sense).
    Non-numeric results pass through unchanged.
    """
    if bounds is None:
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    result = float(value)
    if "min" in bounds:
        result = max(result, bounds["min"])
    if "max" in bounds:
        result = min(result, bounds["max"])
    # Preserve int type when the original was int and result is still whole.
    if isinstance(value, int) and result == int(result):
        return int(result)
    return result


def apply_derived(
    config: DerivedConfig,
    row_context: dict[str, Any],
    *,
    column: str | None = None,
    row_index: int | None = None,
) -> Any:
    """Evaluate the derived expression against one row's values.

    This is a pure function: same config + same row_context -> same output.
    No RNG is involved. Mask mode and gen mode produce identical results
    because derived is a closed-expression computation over the row context.

    Args:
        config: Parsed DerivedConfig with the compiled expression.
        row_context: Mapping of column name to its value for this row.
        column: Column name for diagnostics; included in error messages.
        row_index: Row position for diagnostics; included in error messages.

    Returns:
        The expression result, optionally clipped by bounds, or None when
        null_propagation is "explicit_null" and any referenced column is
        None/NaN.

    Raises:
        ValueError: Expression evaluation raised an error (e.g. ZeroDivisionError,
            TypeError). The message names the column and row index so the operator
            can locate the offending data without re-running.
    """
    col_refs = _get_column_refs(config.compiled)
    ctx = _coerce_context(row_context, col_refs, config.null_propagation)
    if ctx is None:
        # explicit_null mode: a referenced column was null/NaN.
        return None
    try:
        result = evaluate(config.compiled, ctx)
    except Exception as exc:
        parts: list[str] = []
        if column is not None:
            parts.append(f"column {column!r}")
        if row_index is not None:
            parts.append(f"row {row_index}")
        ctx_str = ", ".join(parts) if parts else "unknown column"
        raise ValueError(f"derived expression evaluation failed at {ctx_str}: {exc}") from exc
    return _apply_bounds(result, config.bounds)
