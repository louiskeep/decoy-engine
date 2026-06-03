"""Per-op execution for the V2 narrow transform surface.

S17-TX-NARROW: each TransformOp variant maps to a single pure
``apply_transform(df, op) -> df`` function that operates on a pandas
DataFrame. The dispatch lives in ``apply_transforms(df, ops)`` which
iterates in declared order. Called by the mask path's
PandasExecutionAdapter between source-read and the strategy loop.

Pandas semantics for expression evaluation: pandas ``DataFrame.eval``
resolves ``@var``-style references BEFORE engine dispatch by walking the
caller's locals + globals (`_replace_locals` in pandas internals). The
numexpr engine pin does NOT prevent that scope-walk; a malicious
expression like ``a + @pd.compat.os.system('touch /tmp/pwned')`` will
execute the side-effecting call even with ``engine='numexpr'`` because
``@pd`` resolves to the module-top ``pd`` import in this file's globals.

We therefore (1) pin ``engine='numexpr'`` to keep perf characteristics
predictable and to raise ``ImportError`` (mapped to ``numexpr_required``)
when the dep is missing, and (2) pass ``local_dict={}`` + ``global_dict={}``
explicitly to clamp the eval scope to the DataFrame's columns. Column
references resolve through pandas's column-scope path, NOT through
locals/globals, so legitimate expressions like ``age >= 18`` still work.
This closes Dennis C1 (2026-05-30 gate review).

References:
- pandas DataFrame.eval docs (local_dict / global_dict parameters)
- QA finding Q16 (2026-05-30) flagged the Python-engine fallback as a
  code-execution vector; Dennis C1 ruled the numexpr pin alone does
  not address it because @var resolution is engine-independent.

Compile-time validators in ``apply_transforms`` reject:
- ``derive.column`` already present (would silently overwrite)
- ``drop_column.columns`` not present (typo would silently no-op)
- ``sort.by`` not present (typo would raise mid-sort)
- ``sort.ascending`` list length mismatched against ``by`` length

These pre-checks fire before the dataframe is touched so the error
surface is "your config is wrong" not "your data is wrong."
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.config._transforms import (
    DedupeOp,
    DeriveOp,
    DropColumnOp,
    FilterOp,
    LimitOp,
    SortOp,
    TransformOp,
)


class TransformError(Exception):
    """Raised by apply_transforms when an op references missing columns or
    would overwrite an existing one. Carries ``code`` so the platform's
    failed-path classifier can route it to the right manifest section.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _apply_filter(df: pd.DataFrame, op: FilterOp) -> pd.DataFrame:
    try:
        # Q16 + Dennis C1 fix: pin engine to numexpr AND clamp the eval
        # scope to the DataFrame's columns. The local_dict/global_dict
        # empties block @var-style scope walks that would otherwise reach
        # module-top imports (e.g. `@pd.compat.os.system(...)`).
        mask = df.eval(op.expression, engine="numexpr", local_dict={}, global_dict={})
    except ImportError as exc:
        raise TransformError(
            code="numexpr_required",
            message=("transforms require numexpr; install it with: pip install numexpr"),
        ) from exc
    except Exception as exc:
        raise TransformError(
            code="filter_expression_error",
            message=f"filter expression {op.expression!r} failed: {type(exc).__name__}",
        ) from exc
    # QA-10 F8 (2026-06-01): accept pandas nullable BooleanDtype too.
    # The pre-fix equality check `mask.dtype != bool` rejected
    # `pd.BooleanDtype()` which arises naturally from numexpr eval over
    # nullable-integer or nullable-boolean columns (the default Arrow ->
    # pandas conversion). `pd.api.types.is_bool_dtype` accepts both
    # numpy bool and pandas nullable BooleanDtype. Same fix shape as
    # QA-3 F4 closure on the masking-side `when_gate`.
    if not isinstance(mask, pd.Series) or not pd.api.types.is_bool_dtype(mask.dtype):
        raise TransformError(
            code="filter_expression_not_boolean",
            message=(
                f"filter expression {op.expression!r} did not yield a boolean Series "
                f"(got {type(mask).__name__})"
            ),
        )
    return df[mask].reset_index(drop=True)


def _apply_sort(df: pd.DataFrame, op: SortOp) -> pd.DataFrame:
    missing = [c for c in op.by if c not in df.columns]
    if missing:
        raise TransformError(
            code="sort_column_missing",
            message=f"sort.by columns not in table: {missing}",
        )
    ascending = op.ascending
    if isinstance(ascending, list) and len(ascending) != len(op.by):
        raise TransformError(
            code="sort_ascending_length_mismatch",
            message=(
                f"sort.ascending length {len(ascending)} does not match by length {len(op.by)}"
            ),
        )
    return df.sort_values(by=op.by, ascending=ascending, kind="stable").reset_index(drop=True)


def _apply_limit(df: pd.DataFrame, op: LimitOp) -> pd.DataFrame:
    return df.head(op.n).reset_index(drop=True)


def _apply_dedupe(df: pd.DataFrame, op: DedupeOp) -> pd.DataFrame:
    if op.columns is not None:
        missing = [c for c in op.columns if c not in df.columns]
        if missing:
            raise TransformError(
                code="dedupe_column_missing",
                message=f"dedupe.columns not in table: {missing}",
            )
    return df.drop_duplicates(subset=op.columns).reset_index(drop=True)


def _apply_derive(df: pd.DataFrame, op: DeriveOp) -> pd.DataFrame:
    if op.column in df.columns:
        raise TransformError(
            code="derive_column_already_exists",
            message=(
                f"derive.column {op.column!r} already exists on the table; "
                "rename it or drop the existing column first."
            ),
        )
    try:
        # Q16 + Dennis C1 fix: see _apply_filter for the full rationale.
        result = df.eval(op.expression, engine="numexpr", local_dict={}, global_dict={})
    except ImportError as exc:
        raise TransformError(
            code="numexpr_required",
            message=("transforms require numexpr; install it with: pip install numexpr"),
        ) from exc
    except Exception as exc:
        raise TransformError(
            code="derive_expression_error",
            message=(
                f"derive expression {op.expression!r} for column {op.column!r} failed: "
                f"{type(exc).__name__}"
            ),
        ) from exc
    # Q21 fix: df.assign() avoids the df.copy() full materialization; pandas
    # internally shares column references for unmodified columns. At 1M rows
    # x 50 columns this is ~200-400 MB savings per derive op.
    return df.assign(**{op.column: result})


def _apply_drop_column(df: pd.DataFrame, op: DropColumnOp) -> pd.DataFrame:
    missing = [c for c in op.columns if c not in df.columns]
    if missing:
        raise TransformError(
            code="drop_column_missing",
            message=f"drop_column.columns not in table: {missing}",
        )
    return df.drop(columns=op.columns)


def apply_transform(df: pd.DataFrame, op: TransformOp) -> pd.DataFrame:
    """Apply a single transform op. Pure: returns a new DataFrame; never mutates."""
    if isinstance(op, FilterOp):
        return _apply_filter(df, op)
    if isinstance(op, SortOp):
        return _apply_sort(df, op)
    if isinstance(op, LimitOp):
        return _apply_limit(df, op)
    if isinstance(op, DedupeOp):
        return _apply_dedupe(df, op)
    if isinstance(op, DeriveOp):
        return _apply_derive(df, op)
    if isinstance(op, DropColumnOp):
        return _apply_drop_column(df, op)
    raise TransformError(
        code="unknown_transform_op",
        message=f"unknown transform op type: {type(op).__name__}",
    )


def apply_transforms(df: pd.DataFrame, ops: list[TransformOp]) -> pd.DataFrame:
    """Apply transforms in declared order; each op sees the prior op's output."""
    for op in ops:
        df = apply_transform(df, op)
    return df
