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
from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution.out_of_core._categorical_native import native_categorical_array
from decoy_engine.execution.out_of_core._fpe_native import native_fpe_array
from decoy_engine.execution.out_of_core._hash_native import BoundHashKernel, native_hash_array
from decoy_engine.execution.out_of_core._mask_group_b import (
    GROUP_B_STRATEGIES,
    categorical_array,
    fpe_array,
    group_b_output_type,
    text_redact_array,
)
from decoy_engine.execution.out_of_core._mask_group_c import (
    GROUP_C_STRATEGIES,
    group_c_array,
    group_c_output_type,
)
from decoy_engine.execution.out_of_core._offset_bridge import (
    OFFSET_BRIDGE_STRATEGIES,
    bridge_windowed_date,
    windowed_date_anchor,
)
from decoy_engine.execution.out_of_core._row_error_bridge import (
    OWN_COLUMN_ROW_ERROR_STRATEGIES,
    bridge_own_column,
)
from decoy_engine.execution.out_of_core._text_redact_native import native_text_redact_array
from decoy_engine.kernel import hash_array, passthrough_array, redact_array, truncate_array

if TYPE_CHECKING:
    from decoy_engine.execution._row_errors import RowErrorRecord
    from decoy_engine.execution.out_of_core._bucket_native import NativeBucket
    from decoy_engine.execution.out_of_core._categorical_native import NativeCategorical
    from decoy_engine.execution.out_of_core._fpe_native import NativeFpe
    from decoy_engine.execution.out_of_core._hash_run import TableHashPlan
    from decoy_engine.execution.out_of_core._text_redact_native import NativeTextRedact
    from decoy_engine.plan._types import ColumnSeed, Plan, TableSeed
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.transforms._codeset_loader import _CorpusRecord


def _bridge_own_column(
    plan: Plan,
    table: str,
    column: str,
    seed: ColumnSeed,
    source_array: pa.Array | pa.ChunkedArray,
    mask_key: bytes,
    registry: ProviderRegistry | None,
    row_offset: int,
) -> tuple[pa.Array, tuple[RowErrorRecord, ...]]:
    """Mask one own-column row-erroring column (bucketize/top_code) via the
    oracle-handler bridge, failing closed on a missing registry (a wiring bug:
    the runner always threads its registry, so this guards a direct caller).

    The bridge needs no per-batch dtype forcing: the compat gate admits a column
    onto this route only when its bare per-batch conversion is provably identical
    to the oracle's whole-frame conversion (`row_error_bridge_batch_stable`), and
    declines the rest to the full-frame oracle."""
    if registry is None:
        raise ExecutionError(
            code="out_of_core_row_error_registry_missing",
            message=(
                f"{table}.{column}: out-of-core {seed.strategy!r} needs a provider registry "
                "for its handler bridge, but none was threaded."
            ),
        )
    return bridge_own_column(
        table,
        column,
        seed,
        source_array,
        registry=registry,
        job_seed=plan.seed_envelope.job_seed,
        mask_key=mask_key,
        row_offset=row_offset,
    )


def _bridge_windowed_date(
    plan: Plan,
    table: str,
    column: str,
    seed: ColumnSeed,
    source_frame: pa.RecordBatch | pa.Table,
    mask_key: bytes,
    registry: ProviderRegistry | None,
    row_offset: int,
) -> pa.Array:
    """Mask one windowed_date column via the row-offset bridge, reading the raw
    anchor from `source_frame` (the gate restricts the anchor to unmasked /
    passthrough, so the raw value is what the oracle reads)."""
    anchor_name = windowed_date_anchor(seed)
    anchor_idx = source_frame.schema.get_field_index(anchor_name) if anchor_name else -1
    if anchor_name is None or anchor_idx < 0:
        raise ExecutionError(
            code="out_of_core_windowed_date_anchor_missing",
            message=(
                f"{table}.{column}: windowed_date anchor column {anchor_name!r} is not in "
                "the source."
            ),
        )
    return bridge_windowed_date(
        table,
        column,
        seed,
        anchor_name,
        source_frame.column(anchor_idx),
        registry=registry,
        job_seed=plan.seed_envelope.job_seed,
        mask_key=mask_key,
        row_offset=row_offset,
    )


def table_seed(plan: Plan, table_name: str) -> TableSeed | None:
    """Return the plan's TableSeed for one table, or None when unplanned."""
    for candidate_name, candidate_seed in plan.seed_envelope.per_table:
        if candidate_name == table_name:
            return candidate_seed
    return None


def truncate_params(cfg: dict[str, Any]) -> tuple[int, str, str | None]:
    """Validated truncate kernel params, or raise on an invalid config.

    Fail-closed, matching the pandas oracle (`_strategies/_truncate.py`): an
    invalid length/keep/mask_char raises `StrategyError` with the SAME codes the
    oracle raises (`truncate_length_invalid`, `truncate_keep_invalid`,
    `truncate_mask_char_invalid`), never a silent fallback. A masking primitive
    must never publish the raw source value on a bad config; centralizing the
    validation here keeps the mask dispatch and the batch join's output typing
    agreeing on exactly when the config is rejected.
    """
    length = cfg.get("length")
    if not isinstance(length, int) or length < 1:
        raise StrategyError(
            code="truncate_length_invalid",
            strategy="truncate",
            message=(
                f"truncate has invalid length {length!r} ({type(length).__name__}); "
                "length must be an integer >= 1."
            ),
        )
    keep = cfg.get("keep")
    if keep is None:
        keep = "tail" if bool(cfg.get("from_end", False)) else "head"
    if keep not in ("head", "tail"):
        raise StrategyError(
            code="truncate_keep_invalid",
            strategy="truncate",
            message=f"truncate has invalid keep {keep!r}.",
        )
    mask_char = cfg.get("mask_char")
    if mask_char is not None and (not isinstance(mask_char, str) or len(mask_char) != 1):
        raise StrategyError(
            code="truncate_mask_char_invalid",
            strategy="truncate",
            message=(
                f"truncate has invalid mask_char {mask_char!r} ({type(mask_char).__name__}); "
                "mask_char must be a single character."
            ),
        )
    return length, keep, mask_char


def mask_column(
    values: pa.Array | pa.ChunkedArray,
    seed: ColumnSeed,
    mask_key: bytes,
    *,
    column: str | None = None,
    corpus_record: _CorpusRecord | None = None,
    sub_floor_notices: dict[str, int] | None = None,
    hash_kernel: BoundHashKernel | None = None,
    categorical_native: NativeCategorical | None = None,
    bucket_native: NativeBucket | None = None,
    fpe_native: NativeFpe | None = None,
    text_redact_native: NativeTextRedact | None = None,
) -> pa.Array:
    """Apply one admitted strategy to one column (or column slice).

    `mask_key` is the DE-02 keyed-mask IKM fed at the one substituted slot: the
    8-byte `job_seed` when no secret is present (byte-identical to pre-DE-02) or a
    32-byte KeyProvider mask root under a secret. Every keyed leaf below
    (fpe/categorical/hash/group_c) receives it in its `job_seed`-named IKM slot.

    `column` carries the column name for strategies whose output depends on it
    (fpe's per-column tweak). It is None only on the FK parent-key / remap call
    paths, which the compat gate restricts to the column-independent strategies
    (hash/redact/truncate/passthrough), so a None column never reaches a
    strategy that needs it.

    `corpus_record` (Codex round-6 P2 MASKING/EVIDENCE VERSION DIVERGENCE
    remediation) is forwarded to the `code_set` Group (c) kernel only -- see
    `_mask_group_c._code_set_array`'s docstring. `sub_floor_notices` (round-2
    MEDIUM-1) is forwarded to the `text_mask` Group (c) kernel only -- see
    `_mask_group_c._text_mask_array`'s docstring. Every other strategy
    ignores both.

    `hash_kernel` is set only by `mask_batch`, for a hash column its table plan decided
    to run on the compiled companion. The FK remap sites never pass it and keep the Python hash.
    `categorical_native` is its categorical counterpart; `categorical_array` stays the reference
    and the path every declined column takes. `bucket_native` is the bucket_perturb counterpart,
    taken inside the Group (c) seam once its own validation has run. `fpe_native` is the fpe
    counterpart; `fpe_array` stays the reference and the path every declined column takes.
    `text_redact_native` is the text_redact counterpart; `text_redact_array` stays the reference
    and the declined path.
    """
    job_seed = mask_key
    cfg = provider_config_to_dict(seed.provider_config)
    if seed.strategy in GROUP_B_STRATEGIES:
        if seed.strategy == "fpe":
            if fpe_native is not None and seed.namespace is not None and column is not None:
                return native_fpe_array(
                    values,
                    native=fpe_native,
                    mask_key=job_seed,
                    namespace=seed.namespace,
                    column=column,
                )
            return fpe_array(
                values, job_seed=job_seed, namespace=seed.namespace, column=column, cfg=cfg
            )
        if seed.strategy == "text_redact":
            if text_redact_native is not None:
                return native_text_redact_array(values, native=text_redact_native)
            return text_redact_array(values, plan=seed, column=column)
        if categorical_native is not None and seed.namespace is not None:
            return native_categorical_array(
                values, native=categorical_native, mask_key=job_seed, namespace=seed.namespace
            )
        return categorical_array(
            values,
            job_seed=job_seed,
            namespace=seed.namespace,
            deterministic=seed.deterministic,
            cfg=cfg,
        )
    if seed.strategy in GROUP_C_STRATEGIES:
        return group_c_array(
            values,
            seed,
            job_seed,
            column=column,
            cfg=cfg,
            corpus_record=corpus_record,
            sub_floor_notices=sub_floor_notices,
            bucket_native=bucket_native,
        )
    if seed.strategy == "passthrough":
        return passthrough_array(values)
    if seed.strategy == "redact":
        return redact_array(values, redact_with=cfg.get("redact_with", "REDACTED"))
    if seed.strategy == "truncate":
        # Fail-closed: truncate_params raises on an invalid config rather than
        # letting the raw column pass through unmasked.
        length, keep, mask_char = truncate_params(cfg)
        return truncate_array(values, length=length, keep=keep, mask_char=mask_char)
    if seed.strategy == "hash":
        if seed.namespace is None:
            raise ExecutionError(
                code="hash_requires_namespace",
                message="hash strategy requires a namespace.",
            )
        raw_truncate = cfg.get("truncate")
        truncate = raw_truncate if isinstance(raw_truncate, int) and raw_truncate > 0 else None
        if hash_kernel is not None:
            return native_hash_array(
                values,
                bound=hash_kernel,
                mask_key=job_seed,
                namespace=seed.namespace,
                truncate=truncate,
            )
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
    if seed.strategy in GROUP_B_STRATEGIES:
        return group_b_output_type(seed, cfg, source_type)
    if seed.strategy in GROUP_C_STRATEGIES:
        return group_c_output_type(seed, cfg)
    if seed.strategy in OWN_COLUMN_ROW_ERROR_STRATEGIES:
        # bucketize/top_code render every coercible cell to a canonical string
        # (a bucket label or a generalized/aggregate label), so a value-bearing
        # column is string. An all-null or empty column infers null-typed and
        # the fixed-schema sink reconciles it, exactly as `redact` does below.
        return pa.string()
    if seed.strategy in OFFSET_BRIDGE_STRATEGIES:
        # windowed_date emits a formatted ISO date string per non-null anchor;
        # an all-null/empty column infers null-typed and the sink reconciles it.
        return pa.string()
    if seed.strategy == "hash":
        return pa.string()
    if seed.strategy == "truncate":
        # Validate fail-closed (raises on an invalid config, exactly as the
        # mask would); a valid truncate always emits string.
        truncate_params(cfg)
        return pa.string()
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
    mask_key: bytes | None = None,
    code_set_corpus_records: dict[str, _CorpusRecord] | None = None,
    sub_floor_totals: dict[str, dict[str, int]] | None = None,
    registry: ProviderRegistry | None = None,
    row_error_sink: list[RowErrorRecord] | None = None,
) -> pa.Table:
    """Whole-table, whole-column masking.

    The runner masks per batch (`mask_batch`); this is the single-shot
    lowering it must reproduce, retained as the executable definition the
    parity suites pin the streaming path against.

    `mask_key` (DE-02) is the keyed-mask IKM; None falls back to `job_seed`
    (byte-identical to pre-DE-02).

    `code_set_corpus_records` (Codex round-6 P2 MASKING/EVIDENCE VERSION
    DIVERGENCE remediation), keyed by column name: the pinned `_CorpusRecord`
    a `code_set` column should mask off, resolved once by the caller. `None`
    (the default) falls back to a fresh per-value resolve, matching pre-fix
    behavior -- this whole-table entry point runs the values of one column in
    one shot, so it has no cross-call divergence surface of its own; the
    parameter exists so a caller that already pinned records for `mask_batch`
    can pass the SAME dict here too (e.g. a parity test comparing both).

    `sub_floor_totals` (round-2 MEDIUM-1), keyed by column name: a mutable
    `{detector_id: count}` accumulator per `text_mask` column, reused across
    every call the caller makes so a multi-batch stream aggregates into ONE
    dict per column (see `_mask_group_c.text_mask_sub_floor_warning`, which
    the caller emits from this after the whole table/stream is masked).
    `None` (the default) means the caller does not want the warning (this
    single-shot whole-table path resolves the whole column in one call, so it
    has no cross-call aggregation need of its own).
    """
    seed = table_seed(plan, table_name)
    if seed is None:
        return table
    key = mask_key if mask_key is not None else plan.seed_envelope.job_seed
    records = code_set_corpus_records or {}
    out = table
    for column, column_seed in seed.per_column:
        if column in skip_columns:
            continue
        if column not in out.column_names:
            continue
        if column_seed.strategy in OWN_COLUMN_ROW_ERROR_STRATEGIES:
            # Whole-table row_offset is 0: this single-shot path is the parity
            # reference, and a row-error index here is whole-table absolute.
            masked_array, row_error_records = _bridge_own_column(
                plan, table_name, column, column_seed, out.column(column), key, registry, 0
            )
            if row_error_sink is not None:
                row_error_sink.extend(row_error_records)
            out = out.set_column(out.schema.get_field_index(column), column, masked_array)
            continue
        if column_seed.strategy in OFFSET_BRIDGE_STRATEGIES:
            # Whole-table row_offset is 0; the anchor is read from the raw input
            # `table` (unmasked/passthrough per the gate), not progressively-masked `out`.
            masked_array = _bridge_windowed_date(
                plan, table_name, column, column_seed, table, key, registry, 0
            )
            out = out.set_column(out.schema.get_field_index(column), column, masked_array)
            continue
        notices = (
            sub_floor_totals.setdefault(column, {})
            if sub_floor_totals is not None and column_seed.strategy == "text_mask"
            else None
        )
        masked = mask_column(
            out.column(column),
            column_seed,
            key,
            column=column,
            corpus_record=records.get(column),
            sub_floor_notices=notices,
        )
        out = out.set_column(out.schema.get_field_index(column), column, masked)
    return out


def mask_batch(
    plan: Plan,
    table_name: str,
    batch: pa.RecordBatch,
    *,
    skip_columns: frozenset[str] = frozenset(),
    mask_key: bytes | None = None,
    code_set_corpus_records: dict[str, _CorpusRecord] | None = None,
    sub_floor_totals: dict[str, dict[str, int]] | None = None,
    hash_plan: TableHashPlan | None = None,
    registry: ProviderRegistry | None = None,
    row_offset: int = 0,
    row_error_sink: list[RowErrorRecord] | None = None,
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

    `code_set_corpus_records` (Codex round-6 P2 MASKING/EVIDENCE VERSION
    DIVERGENCE remediation), keyed by column name: the pinned `_CorpusRecord`
    a `code_set` column should mask off. The runner resolves this ONCE per
    table, before the first batch streams, and passes the SAME dict to every
    `mask_batch` call for that table -- without it, a customer corpus file
    replaced mid-stream could make a later batch mask off a different corpus
    version than an earlier one, or than the evidence stamped once per table.
    `None` (the default) falls back to a fresh per-value resolve per batch,
    matching pre-fix behavior.

    `sub_floor_totals` (round-2 MEDIUM-1): same accumulator as `mask_table`
    above, but here the whole point is cross-BATCH aggregation -- the runner
    creates ONE dict per table, passes it to every `mask_batch` call for that
    table, and emits the aggregate warning once the whole stream is consumed.

    `hash_plan` carries the table's per-column hash decisions (resolved once before
    the first batch); `None` leaves every hash column on the Python kernel unrecorded.
    """
    seed = table_seed(plan, table_name)
    if seed is None:
        return batch
    key = mask_key if mask_key is not None else plan.seed_envelope.job_seed
    records = code_set_corpus_records or {}
    fields = list(batch.schema)
    arrays = list(batch.columns)
    for column, column_seed in seed.per_column:
        if column in skip_columns:
            continue
        if column_seed.strategy in OWN_COLUMN_ROW_ERROR_STRATEGIES:
            idx = batch.schema.get_field_index(column)
            if idx < 0:
                continue
            masked, row_error_records = _bridge_own_column(
                plan,
                table_name,
                column,
                column_seed,
                arrays[idx],
                key,
                registry,
                row_offset,
            )
            if row_error_sink is not None:
                row_error_sink.extend(row_error_records)
            arrays[idx] = masked
            fields[idx] = pa.field(column, masked.type)
            continue
        if column_seed.strategy in OFFSET_BRIDGE_STRATEGIES:
            idx = batch.schema.get_field_index(column)
            if idx < 0:
                continue
            # The anchor is read RAW (not the progressively-masked `arrays`): the
            # compat gate admits windowed_date only when its anchor is unmasked or
            # passthrough, so the raw value equals what the oracle reads.
            masked = _bridge_windowed_date(
                plan, table_name, column, column_seed, batch, key, registry, row_offset
            )
            arrays[idx] = masked
            fields[idx] = pa.field(column, masked.type)
            continue
        hash_kernel = (
            hash_plan.kernel_for(column, batch)
            if hash_plan is not None and column_seed.strategy == "hash"
            else None
        )
        categorical_native = (
            hash_plan.categorical_for(column, batch)
            if hash_plan is not None and column_seed.strategy == "categorical"
            else None
        )
        bucket_native = (
            hash_plan.bucket_for(column, batch)
            if hash_plan is not None and column_seed.strategy == "bucket_perturb"
            else None
        )
        fpe_native = (
            hash_plan.fpe_for(column, batch)
            if hash_plan is not None and column_seed.strategy == "fpe"
            else None
        )
        text_redact_native = (
            hash_plan.text_redact_for(column, batch)
            if hash_plan is not None and column_seed.strategy == "text_redact"
            else None
        )
        idx = batch.schema.get_field_index(column)
        if idx < 0:
            continue
        notices = (
            sub_floor_totals.setdefault(column, {})
            if sub_floor_totals is not None and column_seed.strategy == "text_mask"
            else None
        )
        masked = mask_column(
            arrays[idx],
            column_seed,
            key,
            column=column,
            corpus_record=records.get(column),
            sub_floor_notices=notices,
            hash_kernel=hash_kernel,
            categorical_native=categorical_native,
            bucket_native=bucket_native,
            fpe_native=fpe_native,
            text_redact_native=text_redact_native,
        )
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
