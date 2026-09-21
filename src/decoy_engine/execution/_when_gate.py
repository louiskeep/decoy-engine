"""MG-3 / M3 (2026-05-31): pre-strategy `when:` predicate gate.

Two thin wrappers that evaluate `ColumnSeed.when` against the
column's frame and dispatch the underlying strategy ONLY to the rows
where the predicate is True. Rows where the predicate is False
passthrough untouched.

`run_with_when_gate` is used by the pandas execution adapter.

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

import logging
import warnings
from collections.abc import Sequence
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import (
        StrategyContext,
        StrategyHandler,
    )
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.plan._types import ColumnSeed


def _remap_gated_row_errors(
    ctx: StrategyContext,
    err_start: int,
    full_positions: Sequence[int],
) -> None:
    """Rewrite subset-relative RowError indices to full-table positions.

    BLOCKER B1 (dennis review 2026-07-04): the `when:` gate hands the
    handler a SUBSET frame (`df.loc[mask]` / `frame.filter(mask)`), so the
    handler records ``RowError.row_index`` positional in that subset. The
    pipeline + quarantine machinery (`_pipeline.py` D8, `quarantine.py` D9)
    consume ``row_index`` as a FULL-TABLE position. Left unremapped, a gated
    format/mask error quarantines/deletes the WRONG row and ships the raw
    source value of the real bad row -- exactly the silent leak this sprint
    exists to kill.

    Only the errors appended during THIS handler call (indices
    ``err_start..``) are remapped; ``RowError`` is frozen, so each entry is
    replaced in place with a copy carrying the full-table index. Preserving
    the sink's identity is required (the adapter drains it by reference,
    trap T6). ``full_positions[k]`` is the full-table row position of the
    k-th gated (mask-True) row, in mask order.
    """
    for j in range(err_start, len(ctx.row_errors)):
        e = ctx.row_errors[j]
        ctx.row_errors[j] = RowError(
            column=e.column,
            row_index=int(full_positions[e.row_index]),
            trigger=e.trigger,
            reason=e.reason,
        )


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
        # Audit L1 (2026-06-12): same fallback surfacing as
        # execution/_transforms._eval_clamped -- pandas silently drops
        # to the python engine on extension-array dtypes with only an
        # unmonitored RuntimeWarning.
        with warnings.catch_warnings(record=True) as _caught:
            warnings.simplefilter("always", RuntimeWarning)
            mask = pdf.eval(
                expression,
                engine="numexpr",
                local_dict={},
                global_dict={},
            )
        for _w in _caught:
            if issubclass(_w.category, RuntimeWarning):
                _log.warning(
                    "when expression %r: numexpr fell back to the python engine (%s)",
                    expression,
                    _w.message,
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

    # Codex P2 FAIL-CLOSED VALIDATION BYPASSED BY A ZERO-MATCH `when` GATE
    # remediation: a handler's own fail-closed preflight (e.g. CodeSetHandler
    # loading/validating its corpus) normally runs inside `handler.run()`,
    # which the `not mask.any()` short-circuit below never reaches. That let
    # a column referencing a missing/invalid corpus succeed silently
    # whenever its `when:` predicate happened to match zero rows. A handler
    # that needs config/data validated regardless of match count exposes an
    # optional `preflight(plan, ctx) -> None` method; call it unconditionally
    # here, BEFORE the short-circuit, so it always runs while the handler's
    # own `run()` (reached only on a non-empty match) still does the real
    # per-row work and evidence stamping unchanged.
    preflight = getattr(handler, "preflight", None)
    if preflight is not None:
        preflight(plan, ctx)

    mask = _eval_predicate(df, plan.when, plan.strategy)

    if not mask.any():
        return df, []

    sub_df = df.loc[mask].copy()
    err_start = len(ctx.row_errors)
    sub_df, warnings = handler.run(sub_df, column, plan, ctx)
    # B1: remap subset-relative row-error indices to full-table positions
    # BEFORE they leave the gate (see _remap_gated_row_errors). The k-th
    # mask-True row's full-table position is np.flatnonzero(mask)[k], and
    # the handler records positions 0..len(sub_df)-1 into the subset in the
    # same order (our row-error producers preserve row order).
    if len(ctx.row_errors) > err_start:
        # `.tolist()` (only on the rare error path) gives a plain Sequence.
        _remap_gated_row_errors(ctx, err_start, np.flatnonzero(mask.to_numpy()).tolist())
    df.loc[mask, column] = sub_df[column]
    return df, warnings
