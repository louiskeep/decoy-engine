"""Per-op execution for the V2 narrow transform surface.

Each TransformOp variant maps to a single pure ``apply_transform(df, op) -> df``
function that operates on a pandas DataFrame. The dispatch lives in
``apply_transforms(df, ops)`` which iterates in declared order.

Nothing in the masking adapters calls these. ``run_pipeline`` applies a mask
table's transforms once, over the whole table, before any route reads it, through
``_transforms_table.apply_table_transforms`` (full-frame source resolution and
the sequential loader). That wrapper carries the row ordinals in the frame index,
so the ops here keep the index they are given: filter, sort, limit and dedupe
must not reset it, or the source-row lineage is lost.

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

import logging
import warnings
from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.config._transforms import (
    DedupeOp,
    DeriveOp,
    DropColumnOp,
    FilterOp,
    LimitOp,
    SortOp,
    TransformOp,
)
from decoy_engine.errors import DecoyError
from decoy_engine.execution._expression_fingerprint import expression_fingerprint

_log = logging.getLogger(__name__)


def _eval_clamped(df: pd.DataFrame, expression: str) -> object:
    """df.eval pinned to numexpr with a clamped scope, fallback surfaced.

    Audit L1 (2026-06-12): on extension-array dtypes pandas silently
    falls back from numexpr to the python engine and emits an
    unmonitored RuntimeWarning -- the Q16 sandbox posture (numexpr
    pinned) degrades without anyone seeing it. Transforms have no
    QualityWarning return channel, so the fallback is captured and
    re-emitted through the engine logger where job logs pick it up.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        result = df.eval(expression, engine="numexpr", local_dict={}, global_dict={})
    for w in caught:
        if issubclass(w.category, RuntimeWarning):
            _log.warning(
                "transform expression sha256:%s: numexpr fell back to the python engine (%s)",
                expression_fingerprint(expression),
                w.message,
            )
    return result


class TransformError(DecoyError):
    """Raised by apply_transforms when an op references missing columns or
    would overwrite an existing one. Carries ``code`` so the platform's
    failed-path classifier can route it to the right manifest section.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def stored_index_fields(schema: pa.Schema) -> set[str]:
    """Physical fields the `pandas` metadata marks as index columns."""
    meta = schema.pandas_metadata or {}
    return {c for c in meta.get("index_columns", []) if isinstance(c, str)}


def _structured_references(table_config: Mapping[str, Any], config: Mapping[str, Any]) -> set[str]:
    """Names a structured config reference resolves to a SOURCE binding.

    Walks the ops in order: a `derive` target is a definition (it introduces a
    derived binding), and a later structured reference to that name resolves to
    the binding, not to a source field. Names still bound to `derived` after the
    last op are excluded from the post-transform references (mask columns,
    relationship keys, group_by anchors).
    """
    refs: set[str] = set()
    derived: set[str] = set()
    for op in table_config.get("transforms") or []:
        kind = op.get("op")
        if kind == "sort":
            refs |= {c for c in op.get("by") or [] if c not in derived}
        elif kind == "dedupe":
            refs |= {c for c in op.get("columns") or [] if c not in derived}
        elif kind == "drop_column":
            for col in op.get("columns") or []:
                if col in derived:
                    derived.discard(col)
                else:
                    refs.add(col)
        elif kind == "derive":
            derived.add(op["column"])
    final: set[str] = set()
    for col in table_config.get("columns") or []:
        final.add(col["name"])
        if col.get("strategy") in ("date_shift", "group_key"):
            group_by = (col.get("provider_config") or {}).get("group_by")
            if isinstance(group_by, str) and group_by:
                final.add(group_by)
    name = table_config.get("name")
    for rel in config.get("relationships") or []:
        ends = [rel.get("parent") or {}, *(rel.get("children") or [])]
        for end in ends:
            if end.get("table") == name:
                final.update(end.get("columns") or [])
    return refs | (final - derived)


def check_transform_source_schema(
    config: Mapping[str, Any], table_name: str, schema: pa.Schema
) -> None:
    """Schema-only guard for a transform-bearing table; reads no values.

    Rejects duplicate physical field names and any STRUCTURED config reference to
    a stored pandas index field. Free-text filter and derive expressions are not
    parsed: the stored index never reaches the frame, so an expression naming it
    fails during evaluation with the existing expression error codes.
    """
    names = list(schema.names)
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise TransformError(
            code="duplicate_source_field_names",
            message=f"table {table_name!r} has duplicate physical field names {dupes}",
        )
    stored = stored_index_fields(schema)
    if not stored:
        return
    table_config = next(
        (t for t in config.get("tables") or [] if t.get("name") == table_name), {"name": table_name}
    )
    hit = sorted(stored & _structured_references(table_config, config))
    if hit:
        raise TransformError(
            code="config_references_stored_index",
            message=(
                f"table {table_name!r}: config references {hit}, which the source stores as "
                "a pandas index field. Transforms discard the stored index, so the "
                "reference would not resolve; reference a data column instead."
            ),
        )


def reject_config_references_stored_index(
    config: Mapping[str, Any], table: str, schema: pa.Schema, registry: Any
) -> None:
    """Refuse a masked table whose config names a stored pandas index field.

    The chunked oracle consumes the stored index as the frame index, so a configured
    column of that name, or a sibling or `when:` reference to it, would not resolve.
    A configured entry named like the field, or any registry-bound `column_access`
    read or write of it (NFKC-normalized like `read_set`), raises
    `config_references_stored_index`. Only a positive reference refuses: a `reads_unknown`
    declaration (an unparsable predicate) runs on the oracle route as it always did, and a
    `writes_unknown` one (an undeclared strategy or a malformed bundle) is refused earlier by
    `check_chunked_compatibility` with `strategy_not_chunk_safe`. Schema only, no
    values are read.
    """
    stored = stored_index_fields(schema)
    if not stored:
        return
    import unicodedata

    from decoy_engine.execution._column_access import column_access

    table_config: Mapping[str, Any] = next(
        (t for t in config.get("tables") or [] if t.get("name") == table), {}
    )
    entries = [c for c in table_config.get("columns") or [] if isinstance(c, dict)]
    referenced = {c["name"] for c in entries if isinstance(c.get("name"), str)}
    for entry in entries:
        access = column_access(entry, registry)
        referenced |= access.reads | access.writes
    normalized = {unicodedata.normalize("NFKC", n) for n in referenced}
    hit = sorted(
        f for f in stored if f in referenced or unicodedata.normalize("NFKC", f) in normalized
    )
    if hit:
        raise TransformError(
            code="config_references_stored_index",
            message=(
                f"table {table!r}: config references {hit}, which the source stores as a "
                "pandas index field. The chunked masker consumes the stored index, so the "
                "reference would not resolve; reference a data column instead."
            ),
        )


def _apply_filter(df: pd.DataFrame, op: FilterOp) -> pd.DataFrame:
    try:
        # Q16 + Dennis C1 fix: pin engine to numexpr AND clamp the eval
        # scope to the DataFrame's columns. The local_dict/global_dict
        # empties block @var-style scope walks that would otherwise reach
        # module-top imports (e.g. `@pd.compat.os.system(...)`).
        mask = _eval_clamped(df, op.expression)
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
    return df[mask]


_NESTED = (list, tuple, dict, set, np.ndarray)


def _reject_nested_keys(df: pd.DataFrame, columns: Any, *, code: str, op: str) -> None:
    """Nested values (Arrow list/struct/map columns) have no pandas sort order or hash,
    and pandas does not always fail on them: it can compare them by identity. Refuse
    them as keys up front."""
    for col in columns:
        series = df[col]
        if series.dtype != object:
            continue
        first = series.dropna().head(1)
        if len(first) and isinstance(first.iloc[0], _NESTED):
            raise TransformError(
                code=code,
                message=f"{op} cannot use column {col!r}: it holds nested (list/struct/map) values",
            )


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
    _reject_nested_keys(df, op.by, code="sort_unsupported_type", op="sort")
    try:
        return df.sort_values(by=op.by, ascending=ascending, kind="stable")
    except (TypeError, ValueError) as exc:
        raise TransformError(
            code="sort_unsupported_type",
            message=f"sort.by columns {op.by} cannot be ordered: {type(exc).__name__}",
        ) from exc


def _apply_limit(df: pd.DataFrame, op: LimitOp) -> pd.DataFrame:
    return df.head(op.n)


def _apply_dedupe(df: pd.DataFrame, op: DedupeOp) -> pd.DataFrame:
    if op.columns is not None:
        missing = [c for c in op.columns if c not in df.columns]
        if missing:
            raise TransformError(
                code="dedupe_column_missing",
                message=f"dedupe.columns not in table: {missing}",
            )
    keys = op.columns if op.columns is not None else list(df.columns)
    _reject_nested_keys(df, keys, code="dedupe_unsupported_type", op="dedupe")
    try:
        return df.drop_duplicates(subset=op.columns)
    except (TypeError, ValueError) as exc:
        raise TransformError(
            code="dedupe_unsupported_type",
            message=f"dedupe columns cannot be compared: {type(exc).__name__}",
        ) from exc


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
        result = _eval_clamped(df, op.expression)
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
