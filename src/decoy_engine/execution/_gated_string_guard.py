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


def _string_and_number_flags(produced: pd.Series) -> tuple[bool, bool]:
    """(has_string, has_non_string_value) over the non-null values of `produced`.

    Fast paths on pandas' inferred type; only a mixed column pays an element scan.
    """
    if pd.api.types.is_numeric_dtype(produced):
        return False, bool(produced.notna().any())
    kind = pd.api.types.infer_dtype(produced, skipna=True)
    if kind == "string":
        return True, False
    if kind == "empty":
        return False, False
    # Plain Python iteration, not Series.map/.any(): a categorical `produced` keeps its dtype
    # through .map(), and .any() on a categorical Series raises TypeError (even a numeric-only
    # categorical, which is not is_numeric_dtype above and falls through to here).
    values = produced.dropna().tolist()
    has_string = any(isinstance(v, str) for v in values)
    has_other = any(not isinstance(v, str) for v in values)
    return has_string, has_other


def _column_would_be_mixed(source: pd.Series, produced: pd.Series, mask: pd.Series) -> bool:
    """Whether the column after the gated write holds both a string and a number.

    That is exactly the pyarrow reconstruction crash. Unmatched non-null source rows keep their
    numbers; matched rows take `produced`. A nullable-boolean predicate yields <NA> for rows it
    cannot decide, `.loc[mask]` leaves those rows unwritten, and `~NA` stays NA so `.any()`
    would skip them: normalize missing entries to False first.
    """
    has_string, produced_has_other = _string_and_number_flags(produced)
    if not has_string:
        return False
    selected = mask.fillna(False).astype(bool)
    retained_number = bool((~selected & source.notna()).any())
    return retained_number or produced_has_other


def reject_gated_string_on_numeric(
    source: pd.Series,
    produced: pd.Series,
    mask: pd.Series,
    *,
    table: str,
    column: str,
    strategy: str,
) -> None:
    """Raise if a gated run would leave a numeric column holding both strings and numbers.

    `source` is the full column before the write, `produced` the handler's output for the
    gated subset, `mask` the gate. Boolean columns are not numeric here. Not raised: numeric-only
    output (values passed through), and uniformly-string output that replaces every non-null row.
    """
    if not pd.api.types.is_numeric_dtype(source) or pd.api.types.is_bool_dtype(source):
        return
    if not _column_would_be_mixed(source, produced, mask):
        return
    raise ExecutionError(
        code="gated_string_output_on_numeric_unsupported",
        message=(
            f"Column {table}.{column} is a numeric column masked under {strategy!r} with a "
            "`when:` row gate, and the result would hold both strings and numbers (the "
            "strategy produces strings while some rows keep their numbers). One column "
            "cannot hold both, and it fails at reconstruction. Mask the whole column (drop "
            "`when:`), stringify the column upstream, or use a strategy that returns numbers."
        ),
    )


def widen_numeric_target_for_strings(df: pd.DataFrame, column: str, produced: pd.Series) -> None:
    """Cast a numeric target column to object before string output replaces all of it.

    Reached only after `reject_gated_string_on_numeric` passed, so a string `produced` here is
    uniformly string and replaces every non-null row. Casting explicitly avoids relying on
    pandas' deprecated implicit upcast.
    """
    if pd.api.types.is_numeric_dtype(df[column]) and _string_and_number_flags(produced)[0]:
        df[column] = df[column].astype(object)
