"""Policy validation check (Reframe-A).

For every column configured to be masked, verify that the masked
output actually differs from the source. Catches no-op masks: the
config said "mask this column" but the output column is identical to
the source. This is the "did the mask actually fire" question, not
the "did the mask destroy detector patterns" question (which is
``residual_pii.check_residual_pii``'s job).

Three legitimate exceptions to "output must differ from source":

1. ``strategy: passthrough`` -- explicit no-op. Info, not fail.
2. Deterministic strategies where the source happens to be the
   same as the output by coincidence (e.g. an FPE mask of a value
   that happens to map to itself for a given key). Rare; treated as
   warning so the operator can confirm.
3. Source column is constant (all the same value); some strategies
   will produce a constant output too. Info.

Findings are per (table, column). Severity is info / warning / fail
per the rules above.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from decoy_engine.verification.postmask._column_facts import ColumnFacts
from decoy_engine.verification.postmask._stream_input import TableBatches
from decoy_engine.verification.postmask.types import PolicyValidationFinding

# Strategies that are EXPLICITLY allowed to produce identical output.
_NO_OP_BY_DESIGN: frozenset[str] = frozenset({"passthrough"})


def check_policy_validation(
    source_frames: dict[str, pd.DataFrame],
    output_frames: dict[str, pd.DataFrame],
    config: dict[str, Any],
) -> list[PolicyValidationFinding]:
    """Verify every configured mask actually transformed its column.

    Args:
        source_frames: ``{table_name: pre-mask DataFrame}``.
        output_frames: ``{table_name: post-mask DataFrame}``.
        config: the validated pipeline config dict.

    Returns:
        List of PolicyValidationFinding (one per configured column).
    """
    findings: list[PolicyValidationFinding] = []

    for table_cfg in config.get("tables") or []:
        table_name = table_cfg.get("name")
        if not isinstance(table_name, str):
            continue
        if table_name not in source_frames or table_name not in output_frames:
            continue
        src_df = source_frames[table_name]
        out_df = output_frames[table_name]
        for col_cfg in table_cfg.get("columns") or []:
            col_name = col_cfg.get("name")
            strategy = col_cfg.get("strategy")
            if not isinstance(col_name, str) or not isinstance(strategy, str):
                continue
            findings.append(
                _check_one_column(
                    table=table_name,
                    column=col_name,
                    strategy=strategy,
                    src_df=src_df,
                    out_df=out_df,
                )
            )

    return findings


def _check_one_column(
    *,
    table: str,
    column: str,
    strategy: str,
    src_df: pd.DataFrame,
    out_df: pd.DataFrame,
) -> PolicyValidationFinding:
    """Compare source vs output for one column."""
    if column not in src_df.columns or column not in out_df.columns:
        return _missing_column(
            table, column, strategy, column in src_df.columns, column in out_df.columns
        )

    # QA-4 F6 (2026-06-01): reset_index on the source + output frames
    # before pulling the column. The src_col.astype(object).equals(...)
    # comparison below is index-aware; if the caller hands in frames
    # whose indexes differ (e.g. one was filtered by `.loc[mask]` while
    # the other is fresh) the equals() returns False even when the
    # row values match. Pre-fix the docstring said "frame index is
    # assumed aligned" but no caller enforced it.
    src_col = src_df[column].reset_index(drop=True)
    out_col = out_df[column].reset_index(drop=True)
    src_distinct = int(src_col.nunique(dropna=True))
    out_distinct = int(out_col.nunique(dropna=True))

    # Dennis M13 fix (2026-06-01): row-count mismatch is its own
    # finding -- the comparison below assumes aligned indexes, and a
    # row that was dropped during masking becomes invisible to the
    # bytes-identical check. Without this guard a strategy that
    # legitimately dropped rows could surface as "output differs from
    # source as expected" alongside FAIL findings for byte-identical
    # columns, hiding the drop entirely.
    if len(src_col) != len(out_col):
        return _row_count_mismatch(
            table, column, strategy, len(src_col), len(out_col), src_distinct, out_distinct
        )

    # Equality check has to be careful about ordering + dtype. We compare
    # element-wise after coercing both to object so dtype changes don't
    # mask actual value preservation. Frame index is enforced aligned
    # by the reset_index above (QA-4 F6, 2026-06-01).
    # Dennis M11 fix (2026-06-01): a comparison failure (ArrowDtype
    # mismatch, etc) previously fell through to bytes_identical=False
    # + severity='info' "output differs as expected", which is a false
    # clean bill of health for a check that couldn't actually run.
    # Surface the failure as severity='error' so the operator knows
    # the validation didn't conclude.
    try:
        bytes_identical = bool(
            len(src_col) == len(out_col) and src_col.astype(object).equals(out_col.astype(object))
        )
    except Exception as exc:
        return PolicyValidationFinding(
            table=table,
            column=column,
            strategy=strategy,
            severity="error",
            source_distinct=src_distinct,
            output_distinct=out_distinct,
            bytes_identical=False,
            message=(
                f"could not compare source vs output for column "
                f"{column!r}: {type(exc).__name__} (see job log). "
                "Policy validation did not conclude for this column."
            ),
        )

    return _decide(table, column, strategy, src_distinct, out_distinct, bytes_identical)


def _missing_column(
    table: str, column: str, strategy: str, in_source: bool, in_output: bool
) -> PolicyValidationFinding:
    return PolicyValidationFinding(
        table=table,
        column=column,
        strategy=strategy,
        severity="error",
        message=(
            f"column {column!r} not present in source ({in_source}) "
            f"or output ({in_output}); cannot validate."
        ),
    )


def _row_count_mismatch(
    table: str,
    column: str,
    strategy: str,
    source_rows: int,
    output_rows: int,
    src_distinct: int,
    out_distinct: int,
) -> PolicyValidationFinding:
    return PolicyValidationFinding(
        table=table,
        column=column,
        strategy=strategy,
        severity="warning",
        source_distinct=src_distinct,
        output_distinct=out_distinct,
        bytes_identical=False,
        message=(
            f"row count mismatch: source has {source_rows} rows, "
            f"output has {output_rows} rows. The mask may have "
            "dropped rows or duplicated them; investigate before "
            "trusting downstream joins."
        ),
    )


def _decide(
    table: str,
    column: str,
    strategy: str,
    src_distinct: int,
    out_distinct: int,
    bytes_identical: bool,
) -> PolicyValidationFinding:
    """The decision table, shared by the frame check and the facts check."""
    if strategy in _NO_OP_BY_DESIGN:
        return PolicyValidationFinding(
            table=table,
            column=column,
            strategy=strategy,
            severity="info",
            source_distinct=src_distinct,
            output_distinct=out_distinct,
            bytes_identical=bytes_identical,
            message=f"strategy {strategy!r} is a no-op by design.",
        )

    if not bytes_identical:
        return PolicyValidationFinding(
            table=table,
            column=column,
            strategy=strategy,
            severity="info",
            source_distinct=src_distinct,
            output_distinct=out_distinct,
            bytes_identical=False,
            message="output differs from source as expected.",
        )

    # Bytes identical when the strategy was NOT no-op-by-design.
    # Constant-source case: if every non-null value is the same, some
    # strategies (e.g. a deterministic mask of constant input) will
    # produce constant output, and bytes_identical can be a coincidence.
    if src_distinct <= 1:
        return PolicyValidationFinding(
            table=table,
            column=column,
            strategy=strategy,
            severity="info",
            source_distinct=src_distinct,
            output_distinct=out_distinct,
            bytes_identical=True,
            message=(
                "source has at most one distinct non-null value; the "
                "mask producing the same output is acceptable."
            ),
        )

    return PolicyValidationFinding(
        table=table,
        column=column,
        strategy=strategy,
        severity="fail",
        source_distinct=src_distinct,
        output_distinct=out_distinct,
        bytes_identical=True,
        message=(
            f"strategy {strategy!r} was configured but the output is "
            "byte-identical to the source. The mask may not have fired."
        ),
    )


def policy_findings(
    config: Mapping[str, Any],
    source_tables: Mapping[str, TableBatches],
    output_tables: Mapping[str, TableBatches],
    facts: Mapping[str, Mapping[str, ColumnFacts]],
) -> list[PolicyValidationFinding]:
    """`check_policy_validation` from column facts: one finding per configured column of a
    table that both sides hold. `facts` is `{table: {column: ColumnFacts}}` over the output."""
    findings: list[PolicyValidationFinding] = []
    for table_cfg in config.get("tables") or []:
        table = table_cfg.get("name")
        if not isinstance(table, str) or table not in source_tables or table not in output_tables:
            continue
        src, out = source_tables[table], output_tables[table]
        for col_cfg in table_cfg.get("columns") or []:
            column, strategy = col_cfg.get("name"), col_cfg.get("strategy")
            if not isinstance(column, str) or not isinstance(strategy, str):
                continue
            in_source, in_output = column in src.schema.names, column in out.schema.names
            if not (in_source and in_output):
                findings.append(_missing_column(table, column, strategy, in_source, in_output))
                continue
            fact = facts[table][column]
            if fact.source_rows != fact.output_rows:
                findings.append(
                    _row_count_mismatch(
                        table,
                        column,
                        strategy,
                        fact.source_rows,
                        fact.output_rows,
                        fact.source_distinct,
                        fact.output_distinct,
                    )
                )
                continue
            findings.append(
                _decide(
                    table,
                    column,
                    strategy,
                    fact.source_distinct,
                    fact.output_distinct,
                    fact.bytes_identical,
                )
            )
    return findings
