"""Execution guard: a `when:`-gated write that would mix strings into a numeric column.

Statically-typed string-output strategies are rejected at plan compile
(`when_string_output_on_numeric_unsupported`). Everything else that can emit strings
(`faker`, whose provider return type is only known by running it, `bucketize` range
labels, and any other pandas-only emitter) reaches `run_with_when_gate`, the single
choke point for in-core gated writes. The realized output of the gated subset is the
first point the type exists. Matched rows would hold strings while unmatched non-null
rows keep their numbers; one Arrow column cannot hold both, and the raw failure is a
late `pyarrow.ArrowTypeError`/`ArrowInvalid` at reconstruction. This raises a coded
error instead, before the write. A gate that selects every non-null row replaces the
whole column with strings and is fine, so it is not rejected.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._errors import ExecutionError


def reject_gated_string_on_numeric(
    source: pd.Series,
    produced: pd.Series,
    mask: pd.Series,
    *,
    table: str,
    column: str,
    strategy: str,
) -> None:
    """Raise if string output from a gated run would be mixed into a numeric column.

    `source` is the full column before the write, `produced` the handler's output for the
    gated subset, `mask` the gate. Boolean columns are not numeric here.
    """
    if not pd.api.types.is_numeric_dtype(source) or pd.api.types.is_bool_dtype(source):
        return
    if pd.api.types.infer_dtype(produced, skipna=True) != "string":
        return
    # A nullable-boolean predicate yields <NA> for rows it cannot decide; `.loc[mask]` leaves
    # those rows unwritten, so they are unmasked. `~NA` stays NA and `.any()` skips it, so
    # normalize missing entries to False first.
    selected = mask.fillna(False).astype(bool)
    if not bool((~selected & source.notna()).any()):
        return  # every non-null row is replaced: the column becomes uniformly string
    raise ExecutionError(
        code="gated_string_output_on_numeric_unsupported",
        message=(
            f"Column {table}.{column} is a numeric column masked under {strategy!r} with a "
            "`when:` row gate that leaves some rows unmasked, and the strategy produces "
            "strings. Matched rows would be strings and unmatched rows numbers in one "
            "column, which fails at reconstruction. Mask the whole column (drop `when:`), "
            "stringify the column upstream, or use a strategy that returns numbers."
        ),
    )


def widen_numeric_target_for_strings(df: pd.DataFrame, column: str, produced: pd.Series) -> None:
    """Cast a numeric target column to object before string output replaces all of it.

    Reached only after `reject_gated_string_on_numeric` passed, i.e. every non-null row is
    replaced. Casting explicitly avoids relying on pandas' deprecated implicit upcast.
    """
    if pd.api.types.is_numeric_dtype(df[column]) and (
        pd.api.types.infer_dtype(produced, skipna=True) == "string"
    ):
        df[column] = df[column].astype(object)
