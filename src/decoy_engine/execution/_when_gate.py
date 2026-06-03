"""MG-3 / M3 (2026-05-31): pre-strategy `when:` predicate gate.

Two thin wrappers that evaluate `ColumnSeed.when` against the
column's frame and dispatch the underlying strategy ONLY to the rows
where the predicate is True. Rows where the predicate is False
passthrough untouched.

The pandas variant (`run_with_when_gate`) is used by the pandas
execution adapter; the polars variant (`run_with_when_gate_polars`)
is used by the polars adapter and converts the polars frame to a
pandas DataFrame just for the predicate eval (reusing the same C1
substrate) before subsetting natively in polars. Both variants share
the same eval semantics + error codes so byte-identical parity is
guaranteed by construction.

Security posture (reuses the Dennis C1 patch on `_transforms.py`):
the eval call pins `engine="numexpr"` AND clamps both `local_dict`
and `global_dict` to empty. That blocks `@var`-style scope walks
that would otherwise reach module-top imports (e.g.
`@pd.compat.os.system(...)`). The numexpr backend never falls back
to Python eval, so an undefined name raises
`UndefinedVariableError` instead of executing.

Composition note: `when:` runs BEFORE the strategy. If the underlying
strategy is itself wrapping behavior (e.g. `nested` in MG-3 / M2),
`when:` filters the rows FIRST and then `nested` walks the surviving
rows' JSON. The order is locked in the runner gate (here) and the
combined cell in `tests/integration/test_when_plus_nested.py` pins
that contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd

from decoy_engine.execution._errors import StrategyError

if TYPE_CHECKING:
    import polars as pl

    from decoy_engine.execution._adapter import (
        StrategyContext,
        StrategyHandler,
    )
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.plan._types import ColumnSeed


def _eval_predicate(
    pdf: pd.DataFrame,
    expression: str,
    strategy: str,
) -> pd.Series[bool]:
    """Shared numexpr-pinned, scope-clamped predicate eval.

    Returns the boolean mask Series. Raises `StrategyError` with one
    of three typed codes on failure:
      - `numexpr_required` if numexpr is not installed
      - `when_expression_error` if the expression raises
      - `when_expression_not_boolean` if the result is not a bool
        Series.

    The strategy name is threaded through so the runner can attribute
    the failure when it bubbles up.
    """
    try:
        mask = pdf.eval(
            expression,
            engine="numexpr",
            local_dict={},
            global_dict={},
        )
    except ImportError as exc:
        raise StrategyError(
            code="numexpr_required",
            strategy=strategy,
            message=("when: requires numexpr; install with: pip install numexpr"),
        ) from exc
    except Exception as exc:
        # L2 close (Dennis MG-3 gate, 2026-05-31): keep the original
        # exception chained via `from exc` so engineers can recover the
        # numexpr-internal type from the traceback, but only surface
        # the typed code + the offending expression to the operator-
        # facing message. The internal class name (e.g.
        # NumExpr2.NumExprError) leaks implementation detail.
        raise StrategyError(
            code="when_expression_error",
            strategy=strategy,
            message=(
                f"when expression {expression!r} failed to evaluate; "
                "check column names + comparison syntax"
            ),
        ) from exc

    # QA-3 F4 (2026-05-31): accept pandas nullable BooleanDtype too.
    # The pre-fix check `mask.dtype != bool` rejected `pd.BooleanDtype()`
    # which arises naturally from Arrow-backed columns and from any
    # boolean expression over a column with NaN. `is_bool_dtype` covers
    # both numpy bool and pandas nullable BooleanDtype.
    if not isinstance(mask, pd.Series) or not pd.api.types.is_bool_dtype(mask.dtype):
        raise StrategyError(
            code="when_expression_not_boolean",
            strategy=strategy,
            message=(
                f"when expression {expression!r} did not produce a "
                f"boolean Series (got {type(mask).__name__}"
                + (f", dtype={mask.dtype}" if isinstance(mask, pd.Series) else "")
                + ")"
            ),
        )
    return mask


def run_with_when_gate(
    handler: StrategyHandler,
    df: pd.DataFrame,
    column: str,
    plan: ColumnSeed,
    ctx: StrategyContext,
) -> tuple[pd.DataFrame, list[QualityWarning]]:
    """Invoke `handler.run(...)` directly when `plan.when` is None.

    When `plan.when` is set, evaluate the predicate on `df`, run the
    handler on the matching subset, and stitch the result back into
    `df` at those row positions. Rows that do not match are left
    untouched.

    Raises `StrategyError` (code one of: ``numexpr_required``,
    ``when_expression_error``, ``when_expression_not_boolean``) when
    the predicate cannot be evaluated or does not return a boolean
    Series. Bad-expression failure is fatal: the operator should see
    the typed error and fix the plan rather than silently
    passing-through a misconfigured gate.
    """
    if plan.when is None:
        return handler.run(df, column, plan, ctx)

    mask = _eval_predicate(df, plan.when, plan.strategy)

    if not mask.any():
        return df, []

    sub_df = df.loc[mask].copy()
    sub_df, warnings = handler.run(sub_df, column, plan, ctx)
    df.loc[mask, column] = sub_df[column]
    return df, warnings


def run_with_when_gate_polars(
    handler: Any,
    frame: pl.DataFrame,
    column: str,
    plan: ColumnSeed,
    ctx: StrategyContext,
) -> tuple[pl.DataFrame, list[QualityWarning]]:
    """Polars-frame counterpart of `run_with_when_gate`.

    For the predicate eval we hand the polars frame to pandas (the
    eval expression syntax is pandas/numexpr, not polars-expression).
    That conversion is the ONLY extra cost on the polars path; the
    actual subset + writeback stays in polars.

    Byte-identical to the pandas adapter's gated dispatch by
    construction: same eval substrate, same `mask.any()` short-circuit,
    same subset semantics.
    """
    import polars as pl  # local import keeps the module pandas-only by default

    if plan.when is None:
        return handler.run(frame, column, plan, ctx)

    pdf = frame.to_pandas()
    mask = _eval_predicate(pdf, plan.when, plan.strategy)

    if not mask.any():
        return frame, []

    mask_pl = pl.Series("_when_mask", mask.to_numpy(), dtype=pl.Boolean)
    # QA-3 F13 (2026-05-31): carry an explicit positional anchor through
    # the subset so the writeback survives a handler that reorders /
    # sorts rows internally. Pre-fix the writeback used
    # `sub_pdf[column].values` (a positional, zero-indexed read), which
    # is label-aligned to mask-true rows only IFF the handler preserved
    # the subset's row order. No current polars handler sorts; this is
    # a contract tightening to prevent a future handler from silently
    # misaligning the writeback. The anchor column is stripped before
    # the writeback so it never leaks into the masked frame.
    anchor_col = "_decoy_when_row_pos"
    positions = pl.Series(anchor_col, range(frame.height), dtype=pl.Int64)
    frame_with_anchor = frame.with_columns(positions)
    sub_frame = frame_with_anchor.filter(mask_pl)
    sub_frame, warnings = handler.run(sub_frame, column, plan, ctx)

    # Stitch via pandas. The eval already paid a `.to_pandas()` on the
    # full frame; we reuse `pdf` and write back to the rows the anchor
    # column says we filtered to. The anchor is the original positional
    # index; even if the handler reordered rows, `set_index` re-aligns
    # the masked values to the destination rows correctly.
    sub_pdf = sub_frame.to_pandas()
    # Drop the anchor from the surface that gets returned to the caller
    # but keep it in sub_pdf to drive label-aligned assignment.
    pdf.iloc[
        sub_pdf[anchor_col].to_numpy(),
        pdf.columns.get_loc(column),
    ] = sub_pdf[column].to_numpy()
    masked_col = pl.from_pandas(pdf[[column]]).get_column(column)
    return frame.with_columns(masked_col), warnings
