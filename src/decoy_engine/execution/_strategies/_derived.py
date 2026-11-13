"""derived strategy handler (engine-v2 SP-10 / P5.S.derived, 2026-06-28).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.derived``.
Core logic (Lark expression evaluation, null propagation, bounds clipping,
column-ref validation) lives in the transforms module for testability and
reuse outside the execution layer.

Config keys accepted via ``plan.provider_config``:
  expression         str   Required. Closed-grammar expression referencing
                           same-table column names as bare identifiers.
  null_propagation   str   Optional. One of "explicit_null" (default),
                           "sentinel", or "default".
  bounds             dict  Optional. {"min": <num>, "max": <num>} clips
                           numeric output.

Mask mode: derived evaluates the expression against exactly its compiled column
references, read from the live per-table frame by real column name (plain tuples from
`df[refs]`, never `itertuples()._asdict()`, whose namedtuple renames a non-identifier or
underscore-led column to `_<position>` and so let an expression read an undeclared column
through a positional alias). A frame with duplicate column labels is refused with the
shared `duplicate_source_field_names` refusal, a reference absent from the frame with
`derived_ref_missing` before any row, and a per-row failure is re-raised as
`derived_evaluation_failed` with the value-free text of `apply_derived`. A
cross-column ordering edge (order_work) runs a masked driver column BEFORE this
one, so a referenced sibling is read at its MASKED value deterministically (a
never-masked sibling reads its unchanged source value).
Gen mode: generation/_synthesize_derived._derived_generate evaluates against
already-generated sibling columns declared before this one in generate_columns
(declared-order sequential semantics). Both modes call apply_derived
from decoy_engine.transforms.derived, which narrows the context to the compiled refs.

Validation timing:
  Expression syntax: parse time (DerivedConfig.from_dict via compile_expr).
  Column-ref existence and cycle detection: plan-compile time (check_derived_
  column_refs in plan/_checks.py), before any execution.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine import reasons
from decoy_engine.execution import _guards
from decoy_engine.execution._adapter import StrategyContext, StrategyOption, provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.expressions import column_refs
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.derived import DerivedConfig, apply_derived


class DerivedStrategyHandler:
    """Compute each row's value from other columns via a closed-grammar expression.

    Delegates to ``decoy_engine.transforms.derived.apply_derived``.
    Config validation (expression syntax) runs at handler invocation via
    DerivedConfig.from_dict, pre-mutation. Column-ref and cycle validation
    runs at plan-compile time via check_derived_column_refs.

    No per-row RNG is used. Determinism is inherent: same row context ->
    same expression output on every run.
    """

    name: str = "derived"
    namespace_required: bool = False
    join_safe: bool = False
    wizard_options: tuple[StrategyOption, ...] = ()
    offer_in_wizard: bool = False

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg_dict = provider_config_to_dict(plan.provider_config)
        config = DerivedConfig.from_dict(cfg_dict)
        refs = sorted(column_refs(config.compiled))
        # Defense in depth for direct callers: every public entry refuses earlier.
        _guards.refuse_duplicate_field_names(ctx.current_table, df.columns)
        missing = [ref for ref in refs if ref not in df.columns]
        if missing:
            raise ExecutionError(
                code=reasons.runtime.DERIVED_REF_MISSING,
                message=(
                    f"derived column {column!r} references column(s) {missing} that the "
                    "source does not have; nothing was evaluated."
                ),
            )

        out: list[object] = []
        failure: str | None = None
        # A frame projected to zero columns iterates zero rows, so a constant expression
        # gets one empty tuple per row instead.
        rows = df[refs].itertuples(index=False, name=None) if refs else (() for _ in range(len(df)))
        for row_idx, values in enumerate(rows):
            try:
                out.append(
                    apply_derived(
                        config,
                        dict(zip(refs, values, strict=True)),
                        column=column,
                        row_index=row_idx,
                    )
                )
            except ValueError as exc:
                # apply_derived's text is value-free by contract; raise outside the block
                # so the per-row error is not chained as `__context__`.
                failure = str(exc)
                break
        if failure is not None:
            raise ExecutionError(code=reasons.runtime.DERIVED_EVALUATION_FAILED, message=failure)

        df[column] = out
        return df, []
