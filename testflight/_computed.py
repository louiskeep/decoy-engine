"""Computed-column correctness invariant (check_computed_columns).

Split from _invariants.py to keep both modules within the 600-line limit.
Imported and re-exported by _invariants.py for backwards compatibility.

Phase 4 generalization: column-name dispatch replaced by formula-driven
evaluation.  Row-wise formulas are compiled and evaluated via the engine's
closed-grammar parser (compile_expr / evaluate from
decoy_engine.expressions._lark_parser).  Aggregate formulas are detected via
_AGGREGATE_PATTERN and computed in pure Python.  The manifest formula string
is the source of truth; no per-column Python helper functions remain.

Supported formula patterns:
  - Row-wise (engine grammar): arithmetic, comparison, case_when, concat,
    days_between, column references.  compile_expr raises ValidationError if
    the expression is outside the closed grammar.
  - Aggregate (pure Python): sum(<col>), count(<col>), avg(<col>),
    min(<col>), max(<col>).  Detected by _AGGREGATE_PATTERN; evaluated by
    the corresponding Python built-in over the output column.

If compile_expr raises ValidationError for a formula that is not an
aggregate, check_computed_columns raises AssertionError immediately so the
suite fails with a clear message (the _computed generalization hard stop
required by the Phase 4 constraint).
"""

from __future__ import annotations

import re
from typing import Any

from ._spec import ComputedColumnSpec

# Matches aggregate-only formulas: sum(col), count(col), avg(col), min(col),
# max(col).  Case-insensitive.  Full match only (no trailing tokens).
_AGGREGATE_PATTERN: re.Pattern[str] = re.compile(
    r"^\s*(sum|count|avg|min|max)\(\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\)\s*$",
    re.IGNORECASE,
)


def _eval_aggregate(op: str, values: list[Any]) -> float:
    """Compute a scalar aggregate over a list of values.

    Args:
        op: Aggregate operation name (sum, count, avg, min, max).
        values: Column values (None entries skipped for all ops except count).

    Returns:
        Scalar result as float.
    """
    numeric = [float(v) for v in values if v is not None]
    op_lower = op.lower()
    if op_lower == "sum":
        return sum(numeric)
    if op_lower == "count":
        return float(len(numeric))
    if op_lower == "avg":
        return sum(numeric) / len(numeric) if numeric else 0.0
    if op_lower == "min":
        return min(numeric) if numeric else 0.0
    if op_lower == "max":
        return max(numeric) if numeric else 0.0
    raise ValueError(f"Unknown aggregate op: {op!r}")  # unreachable (regex guards)


def check_computed_columns(
    job_name: str,
    spec: list[ComputedColumnSpec],
    result: Any,
) -> str:
    """Assert derived / case_when / derived_aggregate columns are correct.

    For each ComputedColumnSpec the function determines the evaluation mode
    from the formula string:
      - Aggregate mode: formula matches _AGGREGATE_PATTERN (e.g. "sum(amount)").
        Computes the Python aggregate and asserts every output row contains that
        scalar value within 1e-4 tolerance.
      - Row-wise mode: everything else.  Compiles via compile_expr and evaluates
        per row.  Asserts equality within 1e-6 for numeric results, exact equality
        for string/bool results.

    For case_when formulas with branch_count > 0, also asserts that the number
    of distinct non-null output values equals branch_count (branch-coverage
    guard: an unused branch could hide a formula bug).

    The formula string is the source of truth (from the manifest's computed_columns
    section).  No per-column Python helper functions exist; all recomputation is
    expression-driven.  If compile_expr raises ValidationError (expression outside
    the closed grammar), check_computed_columns re-raises as AssertionError with
    a diagnostic so the suite fails clearly.

    Args:
        job_name: Job name for error messages.
        spec: List of ComputedColumnSpec from the manifest invariants.
        result: ExecutionResult carrying all output tables.

    Returns:
        Short evidence string summarising what was verified.

    Raises:
        AssertionError: If a computed value is wrong, a branch is unexercised,
            or the formula is outside the closed grammar (generalization stop).
    """
    from decoy_engine.errors import ValidationError
    from decoy_engine.expressions._lark_parser import compile_expr, evaluate

    checked: list[str] = []

    for cs in spec:
        tbl = result.outputs.get(cs.table)
        assert tbl is not None, (
            f"[{job_name}] computed_columns: table '{cs.table}' not in result.outputs."
        )
        col_dict = tbl.to_pydict()
        assert cs.column in col_dict, (
            f"[{job_name}] computed_columns: column '{cs.column}' not in "
            f"table '{cs.table}'. Available columns: {sorted(col_dict)}."
        )
        out_vals = col_dict[cs.column]

        formula = cs.formula.strip()

        # ----------------------------------------------------------------
        # Aggregate mode: sum/count/avg/min/max over a single source column
        # ----------------------------------------------------------------
        agg_match = _AGGREGATE_PATTERN.match(formula)
        if agg_match:
            op = agg_match.group(1)
            src_col = agg_match.group(2)
            assert src_col in col_dict, (
                f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
                f"aggregate formula references column '{src_col}' which is not in "
                f"the output table. Available columns: {sorted(col_dict)}."
            )
            src_vals = col_dict[src_col]
            scalar = _eval_aggregate(op, src_vals)
            for i, v in enumerate(out_vals):
                if v is None:
                    continue
                assert abs(float(v) - scalar) < 1e-4, (
                    f"[{job_name}] computed_columns: {cs.table}.{cs.column} "
                    f"row {i}: value={v!r}, expected {op}({src_col})={scalar:.6f}. "
                    f"Formula: {formula!r}."
                )
            checked.append(f"{cs.table}.{cs.column}({op}={scalar:.2f},rows={len(out_vals)})")
            continue

        # ----------------------------------------------------------------
        # Row-wise mode: compile and evaluate via the engine expression parser
        # ----------------------------------------------------------------
        try:
            compiled = compile_expr(formula)
        except ValidationError as exc:
            # The Phase 4 hard stop: if the formula is outside the closed grammar,
            # the generalization is incomplete.  Fail the suite with a clear
            # diagnostic rather than silently falling back to hardcoded logic.
            raise AssertionError(
                f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
                f"formula {formula!r} failed to compile with the engine's "
                f"closed-grammar parser.  This indicates a formula string in "
                f"the manifest is not in engine expression syntax.  "
                f"Phase 4 generalization requires all computed_column formulas "
                f"to be either aggregate (sum/count/avg/min/max) or engine-grammar "
                f"row-wise expressions.  Parser error: {exc}"
            ) from exc

        errors: list[tuple[int, Any, Any]] = []
        n_rows = len(out_vals)
        for i in range(n_rows):
            # Build row context from all output columns.
            row_context: dict[str, Any] = {col: col_dict[col][i] for col in col_dict}
            expected = evaluate(compiled, row_context)
            actual = out_vals[i]
            # Numeric comparison with tolerance; string/bool exact.
            try:
                if abs(float(actual) - float(expected)) > 1e-6:
                    errors.append((i, expected, actual))
            except (TypeError, ValueError):
                if actual != expected:
                    errors.append((i, expected, actual))
            if len(errors) >= 5:
                break

        assert not errors, (
            f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
            f"{len(errors)} incorrect row(s) (first 5 shown): {errors}. "
            f"Formula: {formula!r}."
        )

        # Branch-coverage guard: distinct output values must equal branch_count.
        if cs.branch_count > 0:
            distinct = {v for v in out_vals if v is not None}
            assert len(distinct) == cs.branch_count, (
                f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
                f"branch_count={cs.branch_count} but {len(distinct)} distinct "
                f"output value(s) found: {sorted(str(v) for v in distinct)}. "
                f"Expected exactly {cs.branch_count} distinct values (one per "
                f"case_when branch).  An unexercised branch could hide a formula "
                f"bug.  Formula: {formula!r}."
            )
            checked.append(f"{cs.table}.{cs.column}(rows={n_rows},branches={cs.branch_count})")
        else:
            checked.append(f"{cs.table}.{cs.column}(rows={n_rows})")

    return "checked=" + ",".join(checked)
