"""Shared per-column masking for the out-of-core route.

One masking kernel, never re-lowered per consumer: the strategy dispatch here
is the single lowering of the admitted out-of-core strategies (hash, redact,
truncate, passthrough) onto the backend-neutral kernel arrays
(kernel/_scalar.py), the same single-kernel principle the out-of-core design
doc mandates (docs/relationships-out-of-core-sprints.md §0.1). Because every
kernel array is per-value deterministic, masking a row-slice equals the same
rows of a whole-column mask, which is what lets `mask_batch` process one
RecordBatch at a time byte-identically to the whole-table `mask_table` path.

Known caveat for streaming consumers: `redact_array` infers its output type
from the values, so an all-null batch yields a null-typed column where a batch
with values yields the `redact_with` type. A fixed-schema writer over batch
streams must account for that (the FK columns already do, via the batch join's
up-front type resolution).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.kernel import hash_array, passthrough_array, redact_array, truncate_array

if TYPE_CHECKING:
    from decoy_engine.plan._types import ColumnSeed, Plan, TableSeed


def table_seed(plan: Plan, table_name: str) -> TableSeed | None:
    """Return the plan's TableSeed for one table, or None when unplanned."""
    for candidate_name, candidate_seed in plan.seed_envelope.per_table:
        if candidate_name == table_name:
            return candidate_seed
    return None


def truncate_params(cfg: dict[str, Any]) -> tuple[int, str, str | None] | None:
    """Validated truncate kernel params, or None when the config is invalid.

    An invalid config falls back to passthrough (the pandas adapter's lenient
    behavior); centralizing the validation here keeps the mask dispatch and the
    batch join's output typing agreeing on when that fallback happens.
    """
    length = cfg.get("length")
    if not isinstance(length, int) or length < 1:
        return None
    keep = cfg.get("keep")
    if keep is None:
        keep = "tail" if bool(cfg.get("from_end", False)) else "head"
    if keep not in ("head", "tail"):
        return None
    mask_char = cfg.get("mask_char")
    if mask_char is not None and (not isinstance(mask_char, str) or len(mask_char) != 1):
        return None
    return length, keep, mask_char


def mask_column(values: pa.Array | pa.ChunkedArray, seed: ColumnSeed, job_seed: bytes) -> pa.Array:
    """Apply one admitted strategy to one column (or column slice)."""
    cfg = provider_config_to_dict(seed.provider_config)
    if seed.strategy == "passthrough":
        return passthrough_array(values)
    if seed.strategy == "redact":
        return redact_array(values, redact_with=cfg.get("redact_with", "REDACTED"))
    if seed.strategy == "truncate":
        params = truncate_params(cfg)
        if params is None:
            return passthrough_array(values)
        length, keep, mask_char = params
        return truncate_array(values, length=length, keep=keep, mask_char=mask_char)
    if seed.strategy == "hash":
        if seed.namespace is None:
            raise ExecutionError(
                code="hash_requires_namespace",
                message="hash strategy requires a namespace.",
            )
        raw_truncate = cfg.get("truncate")
        truncate = raw_truncate if isinstance(raw_truncate, int) and raw_truncate > 0 else None
        return hash_array(values, seed=job_seed, namespace=seed.namespace, truncate=truncate)
    raise ExecutionError(
        code="out_of_core_strategy_unsupported",
        message=f"strategy {seed.strategy!r} is not supported by the out-of-core runner.",
    )


def masked_output_type(seed: ColumnSeed, source_type: pa.DataType | None = None) -> pa.DataType:
    """The Arrow type `mask_column` emits for a column of `source_type`.

    Resolved from the plan alone so a streaming consumer can fix an output
    schema before any data is seen. `redact` is typed from the constant
    replacement scalar with the same inference `redact_array` uses, so the
    analytic type matches the data-derived one whenever the column carries at
    least one non-null value (an entirely-null column infers null-typed
    instead; per-batch consumers reconcile that with a cast). `source_type`
    may be None only for strategies whose output type does not depend on it.
    """
    cfg = provider_config_to_dict(seed.provider_config)
    if seed.strategy == "hash":
        return pa.string()
    if seed.strategy == "truncate":
        if truncate_params(cfg) is not None:
            return pa.string()
        return _required_source_type(source_type, seed.strategy)
    if seed.strategy == "redact":
        # from_pandas matches redact_array's own inference, which treats
        # degenerate scalars (None, NaN) as nulls rather than typed values.
        return pa.array([cfg.get("redact_with", "REDACTED")], from_pandas=True).type
    if seed.strategy == "passthrough":
        return _required_source_type(source_type, seed.strategy)
    raise ExecutionError(
        code="out_of_core_strategy_unsupported",
        message=f"strategy {seed.strategy!r} is not supported by the out-of-core runner.",
    )


def _required_source_type(source_type: pa.DataType | None, strategy: str) -> pa.DataType:
    if source_type is None:
        raise ExecutionError(
            code="out_of_core_source_schema_required",
            message=(
                f"strategy {strategy!r} passes the source column type through, "
                "which a schema-less batch stream cannot provide up front."
            ),
        )
    return source_type


def mask_table(
    plan: Plan,
    table_name: str,
    table: pa.Table,
    *,
    skip_columns: frozenset[str],
) -> pa.Table:
    """Whole-table, whole-column masking.

    The runner masks per batch (`mask_batch`); this is the single-shot
    lowering it must reproduce, retained as the executable definition the
    parity suites pin the streaming path against.
    """
    seed = table_seed(plan, table_name)
    if seed is None:
        return table
    out = table
    for column, column_seed in seed.per_column:
        if column in skip_columns:
            continue
        if column not in out.column_names:
            continue
        masked = mask_column(out.column(column), column_seed, plan.seed_envelope.job_seed)
        out = out.set_column(out.schema.get_field_index(column), column, masked)
    return out


def mask_batch(
    plan: Plan,
    table_name: str,
    batch: pa.RecordBatch,
    *,
    skip_columns: frozenset[str] = frozenset(),
) -> pa.RecordBatch:
    """Mask one RecordBatch's non-FK columns per the plan.

    Byte-identical to the corresponding row-slice of the whole-table mask (the
    kernels are per-value deterministic), with one exception: an all-null
    batch under a redact strategy emits a null-typed column where the
    whole-table slice carries the `redact_with` type. A fixed-schema consumer
    then fails loudly (ArrowInvalid) rather than corrupts; reconciling that
    schema is the streaming runner's job, per the module docstring.
    `skip_columns` carries the FK child columns whose replacement belongs to
    the join, not the mask.
    """
    seed = table_seed(plan, table_name)
    if seed is None:
        return batch
    fields = list(batch.schema)
    arrays = list(batch.columns)
    for column, column_seed in seed.per_column:
        if column in skip_columns:
            continue
        idx = batch.schema.get_field_index(column)
        if idx < 0:
            continue
        masked = mask_column(arrays[idx], column_seed, plan.seed_envelope.job_seed)
        arrays[idx] = masked
        # Same field semantics as Table.set_column with a bare name: the field
        # type follows the masked array, prior field metadata is not carried.
        fields[idx] = pa.field(column, masked.type)
    return pa.record_batch(arrays, schema=pa.schema(fields, metadata=batch.schema.metadata))


__all__ = [
    "mask_batch",
    "mask_column",
    "mask_table",
    "masked_output_type",
    "table_seed",
    "truncate_params",
]
