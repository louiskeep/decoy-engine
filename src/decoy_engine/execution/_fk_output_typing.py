"""FK output-column typing: one lossless materialization contract for every route.

Extracted from `_pandas_adapter.py` (DE-10 fix-forward, module-size sentry) so the
pandas adapter stays under the orchestration LOC cap. Pure move: no logic,
signature, or behavior change from the original `_pandas_adapter._fk_output_column`.
"""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError


def _fk_output_column(values: list[object], column: str) -> object:
    """Materialize one FK output column losslessly, one contract for every route.

    DE-10 (adversarial review): the full-frame + sequential routes used to assign
    the raw Python list straight back to the child frame (`frame[col] = values`),
    letting pandas infer the column dtype. That silently coerces an integer FK key
    past exactly-representable float precision (> 2**53) to float64 the moment the
    column also holds a float value or a null (e.g. 9007199254740993 ->
    9007199254740992.0) -- a silent referential-integrity drift, not a crash. The
    out-of-core route already rejects/preserves this exact shape, so route choice
    decided whether a large key survived.

    Build the column through the SAME Arrow inference the out-of-core route uses in
    `out_of_core/_join.py::_append_output_batch` -- `pa.array(..., from_pandas=True)`:

    * a mix Arrow cannot reconcile into one array (a matched float parent value
      beside an orphan integer key > 2**53) raises ArrowInvalid/ArrowTypeError, so
      surface the SAME coded `out_of_core_fk_key_dtype_unsupported` rejection
      (reject rather than drift) that route already raises -- one typed error
      across all routes, not a parallel mechanism;
    * an all-integral column (even past 2**53) infers int64 and is kept exact;
      `integer_object_nulls=True` materializes a nullable integer column as object
      Python ints + None (which round-trips back to int64 through the output
      writer's `pa.Table.from_pandas`) instead of the lossy float64 pandas would
      pick for int + null.

    The code name is the FK-key-dtype fail-closed family (born on the out-of-core
    route, hence the `out_of_core_` prefix); reusing it verbatim is what makes the
    rejection identical across routes.
    """
    try:
        arr = pa.array(values, from_pandas=True)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
        raise ExecutionError(
            code="out_of_core_fk_key_dtype_unsupported",
            message=(
                f"FK output column {column!r} mixes key values Arrow cannot "
                "reconcile into one array (e.g. a matched float parent value with "
                "an orphan integer child key beyond exactly-representable float "
                "precision, > 2**53); rejected rather than silently rounded to "
                "float64. Matches the out-of-core route's fail-closed contract "
                "(out_of_core/_join.py::_append_output_batch)."
            ),
        ) from exc
    # `.to_numpy()` keeps the assignment POSITIONAL (like the prior raw-list
    # assignment), avoiding any pandas index-alignment on write-back.
    return arr.to_pandas(integer_object_nulls=True).to_numpy()
