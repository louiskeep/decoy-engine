"""Per-op execution for the V2 narrow transform surface.

S17-TX-NARROW: each TransformOp variant maps to a single pure
``apply_transform(df, op) -> df`` function that operates on a pandas
DataFrame. The dispatch lives in ``apply_transforms(df, ops)`` which
iterates in declared order. Called by the mask path's
PandasExecutionAdapter between source-read and the strategy loop.

Pandas semantics for expression evaluation: pandas ``DataFrame.eval``
uses NumPy's expression engine, NOT CPython ``eval``. The security
profile is bounded by the supported operators (arithmetic + comparison
+ logical) and the column scope; no module imports, no attribute
access on Python objects. The pandas docstring notes the engine's
sandbox boundary; we rely on it instead of `safe_eval` here because
the column-reference style (`age >= 18`) is more natural than the
generic-locals style of safe_eval.

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
        mask = df.eval(op.expression)
    except Exception as exc:
        raise TransformError(
            code="filter_expression_error",
            message=f"filter expression {op.expression!r} failed: {type(exc).__name__}",
        ) from exc
    if not isinstance(mask, pd.Series) or mask.dtype != bool:
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
                f"sort.ascending length {len(ascending)} does not match by length "
                f"{len(op.by)}"
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
        result = df.eval(op.expression)
    except Exception as exc:
        raise TransformError(
            code="derive_expression_error",
            message=(
                f"derive expression {op.expression!r} for column {op.column!r} failed: "
                f"{type(exc).__name__}"
            ),
        ) from exc
    out = df.copy()
    out[op.column] = result
    return out


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
