"""Task 4.4 C2 (extended by Task 4.6 slice 1): direct native operator
dispatch for the shadow-admitted slice strategies.

Calls the low-level native kernels DIRECTLY -- never `run_native_or_oracle_
chunked` (`native/_dispatch.py:431`), which does whole-table admission and
silently falls back to the pandas oracle when that admission fails. Going
through it here would make the shadow's parity proof vacuous: a hash node
that could not run natively would silently mask through the oracle instead
of failing, and the comparison would "pass" against itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pyarrow as pa

from decoy_engine.execution._row_errors import RowError
from decoy_engine.execution.native._bucket_perturb_ext import native_bucket_perturb
from decoy_engine.execution.native._categorical_ext import native_categorical
from decoy_engine.execution.native._chunk_masking import sample_faker_array
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._date_shift_ext import FORMAT_ERROR_REASON, native_date_shift
from decoy_engine.execution.native._group_key_kernel import native_group_key
from decoy_engine.execution.native._kernels_keyed import native_keyed_hash
from decoy_engine.execution.native._kernels_scalar import (
    native_passthrough,
    native_redact,
    native_truncate,
)
from decoy_engine.execution.physical._plan import ExecutionBinding
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_diff_codes import (
    NATIVE_COMPANION_UNAVAILABLE,
    ShadowDifference,
)

if TYPE_CHECKING:
    from decoy_engine.execution.native._index_ext import IndexDerivationKernel
    from decoy_engine.generation.pool import ValuePool

__all__ = ["OperatorCallEvidence", "run_operator"]

_PASSTHROUGH: Final = "native_passthrough"
_REDACT: Final = "native_redact"
_TRUNCATE: Final = "native_truncate"
_KEYED_HASH: Final = "native_keyed_hash"
_FAKER_SELECT: Final = "native_faker_select"
_CATEGORICAL: Final = "native_categorical"
_BUCKET_PERTURB: Final = "native_bucket_perturb"
_GROUP_KEY: Final = "native_group_key"
_DATE_SHIFT: Final = "native_date_shift"


@dataclass
class OperatorCallEvidence:
    """Per-node route evidence the coordinator accumulates across a table's
    batches (design doc section 7's route-evidence record, scoped to what
    the shadow coordinator runs). `compiled_kernel_executed` is set ONLY
    after `native_keyed_hash` or (Task 4.6 slice 1) `derive_index_batch`
    itself returns successfully -- the same positive-evidence contract
    `_chunk_masking._mask_chunk_native` uses for its own `NativeRouteEvidence.
    compiled_kernel_executed` flag -- so a hash or faker node's "the compiled
    kernel really ran" claim is never inferred, only observed.
    """

    planned_operator: str
    actual_operator: str | None = None
    executed: bool = False
    compiled_kernel_executed: bool = False
    batches_run: int = 0


def run_operator(
    array: pa.Array | pa.ChunkedArray,
    *,
    binding: ExecutionBinding,
    ctx: ShadowContext,
    evidence: OperatorCallEvidence,
    pool: ValuePool | None = None,
    index_kernel: IndexDerivationKernel | None = None,
    group_key_sibling: pa.Table | None = None,
    column: str | None = None,
) -> tuple[pa.Array, tuple[RowError, ...]]:
    """Dispatch one batch to `binding`'s bound operator, directly. Raises a
    coded `ShadowDifference(native_companion_unavailable)` -- never falls
    back to the oracle -- when the compiled hash companion is missing or
    ABI-incompatible (C2/C4).

    Returns `(out, row_errors)`. `row_errors` are BATCH-LOCAL `RowError`s
    (0-based within `array`, no table): the coordinator is the layer that knows
    the table and the batch offset, so it attributes and rebases them, the same
    split `drain_row_errors` makes for the oracle's handlers. Only date_shift
    emits any; every other operator returns `()`. `column` is the target column
    name the records carry, required by date_shift.

    `pool` is used only by the faker branch; `index_kernel` by faker AND
    categorical (Phase 5 Track B): the coordinator resolves the pool once per
    node and loads the index kernel once per run, then threads both explicitly
    here so every batch shares the identical verified kernel wrapper (mirroring
    the native chunked route's own preflight-once, thread-through contract).
    """
    cfg = dict(binding.resolved_config)
    row_errors: tuple[RowError, ...] = ()
    if binding.operator_id == _PASSTHROUGH:
        out = native_passthrough(array)
    elif binding.operator_id == _REDACT:
        out = native_redact(array, redact_with=cfg.get("redact_with", "REDACTED"))
    elif binding.operator_id == _TRUNCATE:
        length = cfg.get("length")
        out = native_truncate(
            array,
            length=length if isinstance(length, int) else 0,
            keep=cfg.get("keep", "head"),
            mask_char=cfg.get("mask_char"),
        )
    elif binding.operator_id == _KEYED_HASH:
        if binding.key_binding is None:  # pragma: no cover - C0 always binds this for hash
            raise AssertionError("hash node reached run_operator with no KeyBinding")
        try:
            out = native_keyed_hash(
                array,
                mask_key=ctx.mask_key,
                namespace=binding.key_binding.namespace,
                truncate=cfg.get("truncate"),
                native_threads=ctx.native_threads,
            )
        except CryptoExtensionUnavailableError as exc:
            raise ShadowDifference(
                code=NATIVE_COMPANION_UNAVAILABLE,
                detail=f"operator={binding.operator_id!r}: compiled hash companion unavailable",
            ) from exc
        evidence.compiled_kernel_executed = True
    elif binding.operator_id == _FAKER_SELECT:
        if binding.key_binding is None or binding.pool_binding is None:
            # pragma: no cover - C0 always binds both together for faker
            raise AssertionError("faker node reached run_operator with no KeyBinding/PoolBinding")
        if pool is None or index_kernel is None:
            # pragma: no cover - the coordinator always resolves both before
            # calling run_operator for a faker-bound node
            raise AssertionError(
                "faker node reached run_operator with no resolved pool/index_kernel"
            )
        out = sample_faker_array(
            array,
            pool=pool,
            namespace=binding.key_binding.namespace,
            mask_key=ctx.mask_key,
            index_kernel=index_kernel,
            native_threads=ctx.native_threads,
        )
        evidence.compiled_kernel_executed = True
    elif binding.operator_id == _CATEGORICAL:
        if binding.key_binding is None:  # pragma: no cover - C0 always binds this
            raise AssertionError("categorical node reached run_operator with no KeyBinding")
        if not binding.categorical_deterministic:
            # Runtime determinism assertion (Phase 5 Track B): the native
            # categorical operator is always source-keyed, so an unseeded plan
            # must never reach it. Admission already declines a non-deterministic
            # categorical to the oracle; this fails closed if a wiring bug ever
            # routed one here, rather than silently changing its contract.
            raise AssertionError(
                "categorical node reached run_operator with categorical_deterministic=False"
            )
        if binding.categorical_categories is None:  # pragma: no cover - C0 binds it together
            raise AssertionError(
                "categorical node reached run_operator with no resolved categories"
            )
        if index_kernel is None:  # pragma: no cover - the coordinator loads it first
            raise AssertionError("categorical node reached run_operator with no index_kernel")
        out = native_categorical(
            array,
            categories=binding.categorical_categories,
            cdf=binding.categorical_cdf,
            mask_key=ctx.mask_key,
            namespace=binding.key_binding.namespace,
            index_kernel=index_kernel,
            native_threads=ctx.native_threads,
        )
        evidence.compiled_kernel_executed = True
    elif binding.operator_id == _BUCKET_PERTURB:
        if binding.key_binding is None:  # pragma: no cover - C0 always binds this
            raise AssertionError("bucket_perturb node reached run_operator with no KeyBinding")
        if binding.bucket_perturb_bucket is None or binding.bucket_perturb_date_format is None:
            # pragma: no cover - C0 binds both together with the KeyBinding
            raise AssertionError(
                "bucket_perturb node reached run_operator with no resolved bucket/date_format"
            )
        if index_kernel is None:  # pragma: no cover - the coordinator loads it first
            raise AssertionError("bucket_perturb node reached run_operator with no index_kernel")
        out = native_bucket_perturb(
            array,
            bucket=binding.bucket_perturb_bucket,
            date_format=binding.bucket_perturb_date_format,
            mask_key=ctx.mask_key,
            namespace=binding.key_binding.namespace,
            index_kernel=index_kernel,
            native_threads=ctx.native_threads,
        )
        evidence.compiled_kernel_executed = True
    elif binding.operator_id == _GROUP_KEY:
        # group_key is the one operator whose input is NOT the target column and
        # NOT the bare `array`: the coordinator feeds the SIBLING as a
        # single-column source slice (`batch.select([group_by])`) so its `b"pandas"`
        # schema-metadata sidecar and field name survive for the oracle-equivalent
        # stringify. The derived key is written to the target. The binding carries
        # the resolved length/prefix and the SYNTHESIZED f"group_key/{target}"
        # namespace on its KeyBinding.
        if (
            binding.key_binding is None
            or binding.group_key_group_by is None
            or binding.group_key_length is None
            or group_key_sibling is None
        ):  # pragma: no cover - C0 binds these together for group_key
            raise AssertionError(
                "group_key node reached run_operator with no KeyBinding/group_by/length/sibling"
            )
        try:
            out = native_group_key(
                group_key_sibling,
                length=binding.group_key_length,
                prefix=binding.group_key_prefix or "",
                mask_key=ctx.mask_key,
                namespace=binding.key_binding.namespace,
                native_threads=ctx.native_threads,
            )
        except CryptoExtensionUnavailableError as exc:
            raise ShadowDifference(
                code=NATIVE_COMPANION_UNAVAILABLE,
                detail=f"operator={binding.operator_id!r}: compiled raw-hex companion unavailable",
            ) from exc
        evidence.compiled_kernel_executed = True
    elif binding.operator_id == _DATE_SHIFT:
        out, row_errors = _run_date_shift(
            array, binding=binding, ctx=ctx, index_kernel=index_kernel, column=column
        )
        evidence.compiled_kernel_executed = True
    else:  # pragma: no cover - C0 only ever binds the shadow-admitted operators
        raise AssertionError(f"unbound operator id {binding.operator_id!r}")
    evidence.actual_operator = binding.operator_id
    evidence.executed = True
    evidence.batches_run += 1
    return out, row_errors


def _run_date_shift(
    array: pa.Array | pa.ChunkedArray,
    *,
    binding: ExecutionBinding,
    ctx: ShadowContext,
    index_kernel: IndexDerivationKernel | None,
    column: str | None,
) -> tuple[pa.Array, tuple[RowError, ...]]:
    if binding.key_binding is None:  # pragma: no cover - C0 always binds this
        raise AssertionError("date_shift node reached run_operator with no KeyBinding")
    if (
        binding.date_shift_date_format is None
        or binding.date_shift_min_days is None
        or binding.date_shift_max_days is None
    ):  # pragma: no cover - C0 binds all three together with the KeyBinding
        raise AssertionError(
            "date_shift node reached run_operator with no resolved date_format/min_days/max_days"
        )
    if column is None:
        # The records must name their column; a caller that cannot say which
        # column it is masking cannot attribute a format_error, so fail closed.
        raise AssertionError("date_shift node reached run_operator with no target column")
    if index_kernel is None:  # pragma: no cover - the coordinator loads it first
        raise AssertionError("date_shift node reached run_operator with no index_kernel")
    out, positions = native_date_shift(
        array,
        min_days=binding.date_shift_min_days,
        max_days=binding.date_shift_max_days,
        date_format=binding.date_shift_date_format,
        mask_key=ctx.mask_key,
        namespace=binding.key_binding.namespace,
        index_kernel=index_kernel,
        native_threads=ctx.native_threads,
    )
    errors = tuple(
        RowError(column=column, row_index=i, trigger="format_error", reason=FORMAT_ERROR_REASON)
        for i in positions
    )
    return out, errors
