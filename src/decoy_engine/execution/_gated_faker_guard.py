"""Execution guard: a `when:`-gated faker column that would mix strings into a numeric column.

A faker provider has no declared output type (its return type is only known by running it),
so unlike `truncate`/`fpe`/`categorical` the plan-compile check
`when_string_output_on_numeric_unsupported` cannot type it. The realized output of the gated
subset is the first point the type exists. Matched rows would hold strings while unmatched
non-null rows keep their numbers; one Arrow column cannot hold both, and the raw failure is a
late `pyarrow.ArrowTypeError`/`ArrowInvalid` at reconstruction. This raises a coded error
instead, before the write. A gate that selects every non-null row replaces the whole column
with strings and is fine, so it is not rejected.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._errors import ExecutionError


def reject_gated_faker_string_on_numeric(
    source: pd.Series, produced: pd.Series, mask: pd.Series, *, table: str, column: str
) -> None:
    """Raise if string output from a gated faker run would be mixed into a numeric column.

    `source` is the full column before the write, `produced` the handler's output for the
    gated subset, `mask` the gate. Boolean columns are not numeric here.
    """
    if not pd.api.types.is_numeric_dtype(source) or pd.api.types.is_bool_dtype(source):
        return
    if pd.api.types.infer_dtype(produced, skipna=True) != "string":
        return
    if not bool((~mask & source.notna()).any()):
        return  # every non-null row is replaced: the column becomes uniformly string
    raise ExecutionError(
        code="gated_faker_string_on_numeric_unsupported",
        message=(
            f"Column {table}.{column} is a numeric column masked under 'faker' with a "
            "`when:` row gate that leaves some rows unmasked, and the provider produces "
            "strings. Matched rows would be strings and unmatched rows numbers in one "
            "column, which fails at reconstruction. Mask the whole column (drop `when:`), "
            "stringify the column upstream, or use a provider that returns numbers."
        ),
    )
