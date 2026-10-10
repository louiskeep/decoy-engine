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

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._operator_registry import OPERATORS
from decoy_engine.execution._row_errors import RowError
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._date_shift_ext import FORMAT_ERROR_REASON
from decoy_engine.execution.native._faker_null_mask import faker_missing_mask
from decoy_engine.execution.native._fpe_route import fpe_fail_closed_error
from decoy_engine.execution.native._operator_params import (
    BucketPerturbParams,
    CategoricalParams,
    DateShiftParams,
    FakerParams,
    FpeParams,
    GroupKeyParams,
    HashParams,
    OperatorParams,
    PassthroughParams,
    RedactParams,
    TextMaskParams,
    TextRedactParams,
    TruncateParams,
)
from decoy_engine.execution.native._operator_step import run_kernel_step, run_kernel_step_masked
from decoy_engine.execution.physical._plan import ExecutionBinding
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_diff_codes import (
    NATIVE_COMPANION_UNAVAILABLE,
    OPERATOR_INVARIANT_VIOLATION,
    ShadowDifference,
)
from decoy_engine.generation.pool import GenerationError

if TYPE_CHECKING:
    from decoy_engine.execution.native._index_ext import IndexDerivationKernel
    from decoy_engine.generation.pool import ValuePool

__all__ = [
    "INDEX_KERNEL_INVARIANT_CODES",
    "OperatorCallEvidence",
    "operator_invariant_violation",
    "operator_invariants_fail_loud",
    "run_operator",
]

_PASSTHROUGH: Final = OPERATORS["passthrough"].operator_id
_REDACT: Final = OPERATORS["redact"].operator_id
_TRUNCATE: Final = OPERATORS["truncate"].operator_id
_TEXT_REDACT: Final = OPERATORS["text_redact"].operator_id
_TEXT_MASK: Final = OPERATORS["text_mask"].operator_id
_KEYED_HASH: Final = OPERATORS["hash"].operator_id
_FPE: Final = OPERATORS["fpe"].operator_id
_FAKER_SELECT: Final = OPERATORS["faker"].operator_id
_CATEGORICAL: Final = OPERATORS["categorical"].operator_id
_BUCKET_PERTURB: Final = OPERATORS["bucket_perturb"].operator_id
_GROUP_KEY: Final = OPERATORS["group_key"].operator_id
_DATE_SHIFT: Final = OPERATORS["date_shift"].operator_id

# The `GenerationError` codes every index-kernel consumer raises when the
# compiled `derive_index_batch` result violates its contract (type, length,
# null mask, range). They describe a broken kernel, never a bad input row.
INDEX_KERNEL_INVARIANT_CODES: Final = frozenset(
    {
        "index_batch_type_mismatch",
        "index_batch_length_mismatch",
        "index_batch_null_mask_mismatch",
        "index_batch_out_of_bounds",
    }
)


@contextmanager
def operator_invariants_fail_loud(operator_id: str) -> Iterator[None]:
    """Re-raise a bound operator's own contract failures as a coded
    `ShadowDifference`, which the unified slice surfaces as an invariant error
    instead of quietly rerouting to the oracle. Every other exception (the
    input-domain failures the oracle raises identically, e.g. a Timestamp
    overflow) passes through untouched so the reroute still handles it."""
    try:
        yield
    except (AssertionError, GenerationError) as exc:
        difference = operator_invariant_violation(operator_id, exc)
        if difference is None:
            raise
        raise difference from exc


def operator_invariant_violation(operator_id: str, exc: BaseException) -> ShadowDifference | None:
    """The coded difference for an operator contract failure, or None when
    `exc` is an input-domain error that must keep its own type. A plain
    function rather than inline in the context manager so mutation testing
    can grade it (mutmut skips decorated functions)."""
    if isinstance(exc, AssertionError):
        return ShadowDifference(
            code=OPERATOR_INVARIANT_VIOLATION,
            detail=f"operator={operator_id!r}: dispatch precondition failed",
        )
    if isinstance(exc, GenerationError) and exc.code in INDEX_KERNEL_INVARIANT_CODES:
        return ShadowDifference(
            code=OPERATOR_INVARIANT_VIOLATION,
            detail=f"operator={operator_id!r}: compiled index kernel violated {exc.code}",
        )
    return None


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
    # Rows the operator was handed, summed per batch. `None` until a batch is recorded, so a
    # hand-built record that never counted reads as non-empty and keeps the strict check.
    rows_seen: int | None = None
    # Rows a `when:` mask selected, summed per batch. `None` for a node with no mask, so only
    # a masked node can be exempt from the positive-kernel check when it selected nothing.
    rows_selected: int | None = None


# The two operators whose kernel loads a companion of its own inside the call; the loader's
# failure is a coded decline, never a fallback to the oracle.
_COMPANION_UNAVAILABLE_DETAIL: Final = {
    _KEYED_HASH: "compiled hash companion unavailable",
    _GROUP_KEY: "compiled raw-hex companion unavailable",
    _FPE: "compiled fpe companion unavailable",
}


# The operators whose binding carries no KeyBinding/PoolBinding: `_bound_params` validates only
# the params shape. text_mask is keyed on the mask key, but like text_redact its binding needs no
# namespace-scoped KeyBinding (the mask key flows from `ctx.mask_key` at dispatch, and text_mask
# keys its spans off the matched value, not a column namespace), so it binds the same way.
_UNKEYED_PARAMS: Final = {
    _PASSTHROUGH: PassthroughParams,
    _REDACT: RedactParams,
    _TRUNCATE: TruncateParams,
    _TEXT_REDACT: TextRedactParams,
    _TEXT_MASK: TextMaskParams,
}


def _bound_params(
    binding: ExecutionBinding,
    *,
    pool: ValuePool | None,
    index_kernel: IndexDerivationKernel | None,
    group_key_sibling: pa.Table | None,
    column: str | None,
    job_seed: bytes | None,
) -> OperatorParams:
    """The operator's resolved parameters, after the fail-closed binding checks.

    Each check is a wiring-bug guard (the compiler binds these together and the coordinator
    resolves the pool and kernel first), run in the order that decides which message a
    malformed binding raises.
    """
    operator_id = binding.operator_id
    params = binding.params
    if operator_id in _UNKEYED_PARAMS:
        if not isinstance(params, _UNKEYED_PARAMS[operator_id]):  # pragma: no cover - compile binds
            raise AssertionError(f"{operator_id} node reached run_operator with no resolved params")
    elif operator_id == _KEYED_HASH:
        if binding.key_binding is None:  # pragma: no cover - C0 always binds this for hash
            raise AssertionError("hash node reached run_operator with no KeyBinding")
        if not isinstance(params, HashParams):  # pragma: no cover - C0 binds it with the key
            raise AssertionError("hash node reached run_operator with no resolved params")
    elif operator_id == _FPE:
        if binding.key_binding is None:  # pragma: no cover - C0 always binds this for fpe
            raise AssertionError("fpe node reached run_operator with no KeyBinding")
        if not isinstance(params, FpeParams):  # pragma: no cover - C0 binds it with the key
            raise AssertionError("fpe node reached run_operator with no resolved params")
    elif operator_id == _FAKER_SELECT:
        if binding.key_binding is None or binding.pool_binding is None:
            # pragma: no cover - C0 always binds both together for faker
            raise AssertionError("faker node reached run_operator with no KeyBinding/PoolBinding")
        if pool is None or index_kernel is None:
            # pragma: no cover - the coordinator always resolves both before
            # calling run_operator for a faker-bound node
            raise AssertionError(
                "faker node reached run_operator with no resolved pool/index_kernel"
            )
        if not isinstance(params, FakerParams):  # pragma: no cover - C0 binds it with the key
            raise AssertionError("faker node reached run_operator with no resolved params")
        if params.positional and not job_seed:
            raise AssertionError("positional faker node reached run_operator with no job_seed")
    elif operator_id == _CATEGORICAL:
        if binding.key_binding is None:  # pragma: no cover - C0 always binds this
            raise AssertionError("categorical node reached run_operator with no KeyBinding")
        if index_kernel is None:  # pragma: no cover - the coordinator loads it first
            raise AssertionError("categorical node reached run_operator with no index_kernel")
        if not isinstance(params, CategoricalParams):  # pragma: no cover - C0 binds it with the key
            raise AssertionError("categorical node reached run_operator with no resolved params")
    elif operator_id == _BUCKET_PERTURB:
        if binding.key_binding is None:  # pragma: no cover - C0 always binds this
            raise AssertionError("bucket_perturb node reached run_operator with no KeyBinding")
        if (
            not isinstance(params, BucketPerturbParams)
            or params.bucket is None
            or params.date_format is None
        ):  # pragma: no cover - C0 binds both together with the KeyBinding
            raise AssertionError(
                "bucket_perturb node reached run_operator with no resolved bucket/date_format"
            )
        if index_kernel is None:  # pragma: no cover - the coordinator loads it first
            raise AssertionError("bucket_perturb node reached run_operator with no index_kernel")
    elif operator_id == _GROUP_KEY:
        # group_key is the one operator whose input is NOT the target column and NOT the bare
        # `array`: the coordinator feeds the SIBLING as a single-column source slice
        # (`batch.select([group_by])`) so its `b"pandas"` schema-metadata sidecar and field name
        # survive for the oracle-equivalent stringify. The derived key is written to the target.
        if (
            binding.key_binding is None
            or not isinstance(params, GroupKeyParams)
            or params.group_by is None
            or params.length is None
            or group_key_sibling is None
        ):  # pragma: no cover - C0 binds these together for group_key
            raise AssertionError(
                "group_key node reached run_operator with no KeyBinding/group_by/length/sibling"
            )
    elif operator_id == _DATE_SHIFT:
        if binding.key_binding is None:  # pragma: no cover - C0 always binds this
            raise AssertionError("date_shift node reached run_operator with no KeyBinding")
        if (
            not isinstance(params, DateShiftParams)
            or params.date_format is None
            or params.min_days is None
            or params.max_days is None
        ):  # pragma: no cover - C0 binds all three together with the KeyBinding
            raise AssertionError(
                "date_shift node reached run_operator with no resolved "
                "date_format/min_days/max_days"
            )
        if column is None:
            # The records must name their column; a caller that cannot say which
            # column it is masking cannot attribute a format_error, so fail closed.
            raise AssertionError("date_shift node reached run_operator with no target column")
        if index_kernel is None:  # pragma: no cover - the coordinator loads it first
            raise AssertionError("date_shift node reached run_operator with no index_kernel")
    else:  # pragma: no cover - C0 only ever binds the shadow-admitted operators
        raise AssertionError(f"unbound operator id {operator_id!r}")
    if params is None:  # pragma: no cover - every branch above rejects a missing params
        raise AssertionError(f"{operator_id} node reached run_operator with no resolved params")
    return params


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
    row_offset: int = 0,
    when_mask: pa.Array | None = None,
    source_slice: pa.Table | None = None,
    text_mask_notices: dict[str, int] | None = None,
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
    name the records carry, required by date_shift. `row_offset` is the table-global ordinal
    of `array`'s first row, which only the two position-keyed operators read: they draw by
    `row_offset + i`, so a batch reproduces the whole-frame draw. The position-keyed Faker
    keys on `ctx.job_seed`; every other operator ignores it.

    `source_slice` is the batch as a single-column table with its schema metadata: the position-keyed
    Faker takes its null mask from the oracle's conversion of it (`_faker_null_mask`).

    `pool` is used only by the faker branch; `index_kernel` by faker AND
    categorical (Phase 5 Track B): the coordinator resolves the pool once per
    node and loads the index kernel once per run, then threads both explicitly
    here so every batch shares the identical verified kernel wrapper (mirroring
    the native chunked route's own preflight-once, thread-through contract).
    `when_mask` is the batch's slice of the node's `when:` row mask (non-null booleans). A
    binding that carries a predicate fails closed without one, so a `when` node can never mask
    every row. Only the admitted value-keyed string operators carry a predicate, and they read
    nothing the masked step does not pass.
    group_key's `raw_hex_kernel` is left unset on purpose: the kernel loads it inside the
    call each batch, which is this route's only companion probe, so an empty column still
    declines when the companion is absent.
    """
    if binding.when_expression is not None and when_mask is None:
        raise ShadowDifference(
            code=OPERATOR_INVARIANT_VIOLATION,
            detail=f"operator={binding.operator_id!r}: a when node reached run_operator unmasked",
        )
    positional_faker = isinstance(binding.params, FakerParams) and binding.params.positional
    job_seed = ctx.job_seed if positional_faker else None
    params = _bound_params(
        binding,
        pool=pool,
        index_kernel=index_kernel,
        group_key_sibling=group_key_sibling,
        column=column,
        job_seed=job_seed,
    )
    try:
        if when_mask is not None:
            result = run_kernel_step_masked(
                params,
                array,
                when_mask,
                mask_key=ctx.mask_key,
                native_threads=ctx.native_threads,
                index_kernel=index_kernel,
                pool=pool,
                row_offset=row_offset,
                job_seed=job_seed,
                missing_mask=(
                    faker_missing_mask(source_slice, column)
                    if positional_faker and source_slice is not None and column is not None
                    else None
                ),
            )
        else:
            result = run_kernel_step(
                params,
                array,
                mask_key=ctx.mask_key,
                native_threads=ctx.native_threads,
                index_kernel=index_kernel,
                raw_hex_kernel=None,
                pool=pool,
                sibling=group_key_sibling,
                row_offset=row_offset,
                job_seed=job_seed,
                missing_mask=(
                    faker_missing_mask(source_slice, column)
                    if positional_faker and source_slice is not None and column is not None
                    else None
                ),
            )
    except CryptoExtensionUnavailableError as exc:
        detail = _COMPANION_UNAVAILABLE_DETAIL.get(binding.operator_id)
        if detail is None:
            raise
        raise ShadowDifference(
            code=NATIVE_COMPANION_UNAVAILABLE,
            detail=f"operator={binding.operator_id!r}: {detail}",
        ) from exc
    if text_mask_notices is not None and result.text_mask_notices:
        # Accumulate this batch's sub-floor counts into the caller's per-node dict so the
        # coordinator builds ONE warning per column after the batch loop, matching the oracle's
        # whole-column scope (C6b-i). text_mask raises its own fail-closed StrategyError inside
        # the kernel, so there is no fpe-style error set to map here.
        for detector_id, count in result.text_mask_notices.items():
            text_mask_notices[detector_id] = text_mask_notices.get(detector_id, 0) + count
    if result.fpe_errors:
        # fpe fail-closed KILLS on the first per-row failure (C6a §3d). The raised StrategyError
        # propagates out of the coordinator; the unified slice's generic exception boundary
        # reroutes to the full-frame oracle, which runs the shipped handler and raises the
        # canonical StrategyError (class + code), so exception parity holds by construction.
        raise fpe_fail_closed_error(result.fpe_errors, column or "?")
    # Any compiled batch wins: the evidence object lives across all of a node's batches.
    if result.ran:
        evidence.compiled_kernel_executed = True
    row_errors: tuple[RowError, ...] = ()
    if result.format_error_positions:
        if column is None:  # pragma: no cover - the date_shift guard above requires a column
            raise AssertionError("date_shift node reached run_operator with no target column")
        row_errors = tuple(
            RowError(column=column, row_index=i, trigger="format_error", reason=FORMAT_ERROR_REASON)
            for i in result.format_error_positions
        )
    evidence.actual_operator = binding.operator_id
    evidence.executed = True
    evidence.batches_run += 1
    evidence.rows_seen = (evidence.rows_seen or 0) + len(array)
    if when_mask is not None:
        evidence.rows_selected = (evidence.rows_selected or 0) + (pc.sum(when_mask).as_py() or 0)
    return result.out, row_errors
