"""Computed-column correctness invariant (check_computed_columns).

Split from _invariants.py to keep both modules within the 600-line limit.
Imported and re-exported by _invariants.py for backwards compatibility.

Phase 4 generalization: column-name dispatch replaced by formula-driven
evaluation.  The manifest formula string is the source of truth; no per-column
Python helper functions remain.

TH-1.3 / P0-3 (independent recomputation): the expected value for a row-wise
formula is computed by an INDEPENDENT harness evaluator built on Python's own
``ast`` module (see ``_compile_independent`` / ``_eval_independent`` below),
NOT the engine's ``evaluate`` from ``decoy_engine.expressions._lark_parser``.
Before this change the harness recomputed with the exact evaluator the engine's
``derived`` strategy uses, so a bug in that shared evaluator produced the same
wrong value on both sides and the invariant passed vacuously (a self-graded
computed column).  The engine's ``compile_expr`` is retained only as a SECONDARY
smoke check that the manifest formula is valid engine-grammar; it never supplies
the expected value.

Supported formula patterns:
  - Row-wise (independent ast evaluator): arithmetic (+, -, *, /), comparison
    (==, !=, <, <=, >, >=), ``case_when(cond, val, ..., default)``, string /
    numeric literals, and column references.  Covers every row-wise formula in
    the three jobs.  A form outside this set raises so the gap is loud, never
    a silent fallback to the engine evaluator.
  - Aggregate (pure Python): sum(<col>), count(<col>), avg(<col>),
    min(<col>), max(<col>).  Detected by _AGGREGATE_PATTERN; evaluated by
    the corresponding Python built-in over the output column.

If ``compile_expr`` raises ValidationError for a formula that is not an
aggregate, check_computed_columns raises AssertionError immediately so the
suite fails with a clear message (the _computed generalization hard stop
required by the Phase 4 constraint).
"""

from __future__ import annotations

import ast
import re
from typing import Any

from ._spec import ComputedColumnSpec


class _IndependentEvalError(Exception):
    """The independent harness evaluator cannot parse/evaluate a formula form.

    Raised for any construct outside the closed set the harness supports so the
    gap surfaces loudly instead of silently deferring to the engine evaluator.
    """


# AST node -> Python operator for the independent evaluator.  Deliberately a
# small whitelist: anything not here raises _IndependentEvalError.
def _apply_binop(op: ast.operator, left: Any, right: Any) -> Any:
    if isinstance(op, ast.Mult):
        return left * right
    if isinstance(op, ast.Add):
        return left + right
    if isinstance(op, ast.Sub):
        return left - right
    if isinstance(op, ast.Div):
        return left / right
    raise _IndependentEvalError(f"unsupported binary operator: {type(op).__name__}")


def _apply_compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return bool(left == right)
    if isinstance(op, ast.NotEq):
        return bool(left != right)
    if isinstance(op, ast.Lt):
        return bool(left < right)
    if isinstance(op, ast.LtE):
        return bool(left <= right)
    if isinstance(op, ast.Gt):
        return bool(left > right)
    if isinstance(op, ast.GtE):
        return bool(left >= right)
    raise _IndependentEvalError(f"unsupported comparison operator: {type(op).__name__}")


def _compile_independent(formula: str) -> ast.expr:
    """Parse ``formula`` with Python's own grammar (independent of the engine).

    Returns the root expression node.  Raises _IndependentEvalError if the string
    is not a single Python expression.  ``case_when`` parses as a plain
    ``ast.Call`` (a valid Python call), evaluated by _eval_independent.
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise _IndependentEvalError(f"not a parseable expression: {exc}") from exc
    return tree.body


def _eval_independent(node: ast.expr, row: dict[str, Any]) -> Any:
    """Evaluate an ast expression node against a row context, independently.

    Handles only the closed set of node types the three jobs' row-wise formulas
    use.  Any other node type raises _IndependentEvalError so an unsupported form
    fails loudly rather than deferring to the engine's evaluator.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in row:
            raise _IndependentEvalError(f"column reference {node.id!r} not in row context")
        return row[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_independent(node.operand, row)
    if isinstance(node, ast.BinOp):
        return _apply_binop(
            node.op,
            _eval_independent(node.left, row),
            _eval_independent(node.right, row),
        )
    if isinstance(node, ast.Compare):
        # Single comparison only (a OP b); chained comparisons are not used.
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise _IndependentEvalError("chained comparisons are not supported")
        return _apply_compare(
            node.ops[0],
            _eval_independent(node.left, row),
            _eval_independent(node.comparators[0], row),
        )
    if isinstance(node, ast.Call):
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "case_when":
            fname = func.id if isinstance(func, ast.Name) else type(func).__name__
            raise _IndependentEvalError(f"unsupported function call: {fname!r}")
        args = node.args
        if len(args) < 1 or len(args) % 2 != 1:
            raise _IndependentEvalError(
                "case_when requires an odd number of args: "
                "(cond, val)+ pairs followed by a single default"
            )
        # Evaluate (cond, value) pairs left-to-right; last arg is the default.
        for i in range(0, len(args) - 1, 2):
            if bool(_eval_independent(args[i], row)):
                return _eval_independent(args[i + 1], row)
        return _eval_independent(args[-1], row)
    raise _IndependentEvalError(f"unsupported expression node: {type(node).__name__}")


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
    from decoy_engine.expressions._lark_parser import compile_expr

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
        # Row-wise mode.
        #
        # SECONDARY smoke check: the formula must still compile under the
        # engine's closed grammar (proves the manifest formula is engine-valid).
        # Its result is intentionally discarded; it never supplies an expected
        # value.
        # ----------------------------------------------------------------
        try:
            compile_expr(formula)  # smoke only -- NOT used for the expected value
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

        # PRIMARY correctness: recompute the expected value with the INDEPENDENT
        # harness evaluator (Python ast), sharing zero code with the engine's
        # evaluate().  A bug in the engine's shared evaluator therefore cannot
        # hide behind a circular recomputation (TH-1.3 / P0-3).
        try:
            indep_tree = _compile_independent(formula)
        except _IndependentEvalError as exc:
            raise AssertionError(
                f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
                f"formula {formula!r} is not supported by the independent harness "
                f"evaluator (TH-1.3).  Row-wise correctness must be checked without "
                f"the engine's evaluator; extend testflight._computed's independent "
                f"evaluator to cover this form.  Detail: {exc}"
            ) from exc

        errors: list[tuple[int, Any, Any]] = []
        n_rows = len(out_vals)
        for i in range(n_rows):
            # Build row context from all output columns.
            row_context: dict[str, Any] = {col: col_dict[col][i] for col in col_dict}
            try:
                expected = _eval_independent(indep_tree, row_context)
            except _IndependentEvalError as exc:
                raise AssertionError(
                    f"[{job_name}] computed_columns: {cs.table}.{cs.column} row {i}: "
                    f"independent evaluation of {formula!r} failed: {exc}"
                ) from exc
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
