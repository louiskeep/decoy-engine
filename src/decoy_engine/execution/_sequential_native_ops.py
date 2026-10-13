"""The per-strategy native operators the sequential (SEQ) route can hand a column to.

`SeqNativeRun` (`_sequential_native.py`) is strategy-agnostic: it pins a column's Arrow type,
asks the strategy's `NativeOp` whether the column is admitted, calls its kernel, and reports
its disposition. Everything strategy-specific lives in one frozen descriptor here, so a new
operator is one more entry in a dict and not another copy of the routing.

A descriptor carries:

- `load_kernel`: the one-time, run-scoped companion load (raises
  `CryptoExtensionUnavailableError` when the companion is absent). Each strategy loads its own,
  so one strategy's load failure never degrades another.
- `admit`: CONFIG-aware. It sees the resolved column seed, not only the Arrow type, because a
  configuration can be un-nativeable (fpe `checksum`) whatever the type is.
- `apply`: the kernel call, returning values plus the kernel's own per-row error channel.
- `fail_closed`: turns a non-empty error channel into the strategy's coded exception, raised
  by the seam before any value is returned, so a failed table publishes nothing.
- `warn`: the handler-side residual-warning pass for operators whose kernel computes none.

hash reuses the out-of-core entry points unchanged. fpe reuses the chunked route's compiled FF1
kernel, its checksum-first admission rule (`native/_real_type_admission.py`), its fail-closed
helper and its warning helper (`native/_fpe_route.py`); FF1 itself is not re-implemented here.

Reason codes carry the strategy, the column name and the Arrow type only, never a cell value.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.native._bucket_perturb_ext import native_bucket_perturb
from decoy_engine.execution.native._categorical_ext import native_categorical
from decoy_engine.execution.native._crypto_ext import FpeConfig, FpeConfigError
from decoy_engine.execution.native._fpe_route import fpe_fail_closed_error, fpe_residual_warnings
from decoy_engine.execution.out_of_core._bucket_native import (
    decline_reason as bucket_decline_reason,
)
from decoy_engine.execution.out_of_core._categorical_native import (
    BoundIndexKernel,
)
from decoy_engine.execution.out_of_core._categorical_native import (
    decline_reason as categorical_decline_reason,
)
from decoy_engine.execution.out_of_core._hash_native import (
    BoundHashKernel,
    decline_reason,
    native_hash_array,
)

if TYPE_CHECKING:
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.plan._types import ColumnSeed

HASH = "hash"
FPE = "fpe"
CATEGORICAL = "categorical"
BUCKET_PERTURB = "bucket_perturb"
SEQ_HASH_DISPOSITION_KEY = "sequential_hash"
SEQ_FPE_DISPOSITION_KEY = "sequential_fpe"
SEQ_CATEGORICAL_DISPOSITION_KEY = "sequential_categorical"
SEQ_BUCKET_DISPOSITION_KEY = "sequential_bucket_perturb"


@dataclass(frozen=True)
class NativeResult:
    """A kernel call's output and its per-row errors (empty for a kernel with no error channel)."""

    values: pa.Array
    errors: tuple[Any, ...] = ()


@dataclass(frozen=True)
class NativeOp:
    strategy: str
    disposition_key: str
    load_kernel: Callable[[int | None], Any]
    admit: Callable[[str, pa.DataType, ColumnSeed], str | None]
    apply: Callable[[Any, pa.Array, Any], NativeResult]
    fail_closed: Callable[[tuple[Any, ...], str], Exception] | None = None
    warn: Callable[[list[Any], Any, str], tuple[QualityWarning, ...]] | None = None


@dataclass(frozen=True)
class HashCall:
    namespace: str
    mask_key: bytes
    truncate: int | None


@dataclass(frozen=True)
class FpeCall:
    namespace: str
    mask_key: bytes
    config: FpeConfig
    tweak_column: str


@dataclass(frozen=True)
class CategoricalCall:
    """The resolved deterministic pool the handler already built: string categories and the
    weight CDF (None = uniform). `admit` returns a reason only, so these travel here."""

    namespace: str
    mask_key: bytes
    categories: tuple[str, ...]
    cdf: tuple[int, ...] | None


@dataclass(frozen=True)
class BucketCall:
    """One bucket_perturb column's call: the handler's resolved config, the sentinel mask the
    seam computed before anything else, and an invocation-local `derive_calls` the kernel
    appends to so the seam reports what actually ran."""

    namespace: str
    mask_key: bytes
    bucket: str
    date_format: str
    column: str
    preserved: np.ndarray[Any, Any] | None
    derive_calls: list[int]


@dataclass(frozen=True)
class BoundFpeKernel:
    """The compiled FF1 kernel, loaded once for the run, and the thread budget it runs with."""

    kernel: Any
    native_threads: int | None = None


def _hash_admit(column: str, input_type: pa.DataType, seed: ColumnSeed) -> str | None:
    return decline_reason(column, input_type)


def _hash_apply(bound: BoundHashKernel, values: pa.Array, call: HashCall) -> NativeResult:
    return NativeResult(
        native_hash_array(
            values,
            bound=bound,
            mask_key=call.mask_key,
            namespace=call.namespace,
            truncate=call.truncate,
        )
    )


def _fpe_admit(column: str, input_type: pa.DataType, seed: ColumnSeed) -> str | None:
    """Why an fpe column stays on Python, or None.

    A truthy `checksum` is rejected before anything else, as the chunked admission does: the
    compiled FF1 kernel has no checksum path. A config the handler would raise on (no namespace,
    a duplicate or degenerate charset) also stays Python so the handler raises it as before.
    The type check is exact `string`: `large_string` declines."""
    cfg = provider_config_to_dict(seed.provider_config)
    if cfg.get("checksum"):
        return f"fpe_checksum_not_native:{column}"
    if seed.namespace is None:
        return f"fpe_requires_namespace_stays_python:{column}"
    try:
        FpeConfig.from_mapping(cfg)._resolve()
    except FpeConfigError:
        return f"fpe_config_stays_python:{column}"
    if input_type != pa.string():
        return f"fpe_source_type_not_string:{column}:{input_type}"
    return None


def _fpe_apply(bound: BoundFpeKernel, values: pa.Array, call: FpeCall) -> NativeResult:
    result = bound.kernel.run(
        values,
        mask_key=call.mask_key,
        namespace=call.namespace,
        tweak_column=call.tweak_column,
        config=call.config,
        forward=True,
        native_threads=bound.native_threads,
    )
    out = result.values
    if not isinstance(out, pa.Array) or out.type != pa.string() or len(out) != len(values):
        raise ExecutionError(
            code="sequential_fpe_native_result_invalid",
            message="the native fpe kernel returned an array that is not one string per input row.",
        )
    return NativeResult(out, tuple(result.errors))


def _fpe_warn(values: list[Any], call: FpeCall, column: str) -> tuple[QualityWarning, ...]:
    # The kernel computes no warnings; run the handler's own pass on the ORIGINAL values at
    # handler scope, so a `when` subset is counted exactly as the Python path counts it.
    return fpe_residual_warnings(values, config=call.config, column=column)


def _categorical_admit(column: str, input_type: pa.DataType, seed: ColumnSeed) -> str | None:
    """Why a categorical column stays on Python, or None.

    A thin adapter over the out-of-core admission (`decline_reason`, reused unchanged): a
    one-field schema stands in for the table, so the same predicate that gates the chunked and
    out-of-core routes gates this one. Only the deterministic, namespaced, string-category,
    exact-`string`-source subset with a buildable CDF is admitted; a non-deterministic config
    declines, so the positional kernel is never used here."""
    _prepared, reason = categorical_decline_reason(
        column, pa.schema([pa.field(column, input_type)]), seed
    )
    return reason


def _categorical_apply(
    bound: BoundIndexKernel, values: pa.Array, call: CategoricalCall
) -> NativeResult:
    return NativeResult(
        native_categorical(
            values,
            categories=call.categories,
            cdf=call.cdf,
            mask_key=call.mask_key,
            namespace=call.namespace,
            index_kernel=bound.kernel,
            native_threads=bound.native_threads,
        )
    )


def _bucket_admit(column: str, input_type: pa.DataType, seed: ColumnSeed) -> str | None:
    """Why a bucket_perturb column stays on Python, or None.

    A thin adapter over the out-of-core admission (`decline_reason`, reused unchanged, and
    through it the route-neutral `bucket_perturb_config_rejection`): a one-field schema stands
    in for the table. A config the handler would raise on (no namespace, invalid
    `preserve_values`) also declines, so the handler raises its own coded error on Python."""
    return bucket_decline_reason(column, column, pa.schema([pa.field(column, input_type)]), seed)


def _bucket_apply(bound: BoundIndexKernel, values: pa.Array, call: BucketCall) -> NativeResult:
    return NativeResult(
        native_bucket_perturb(
            values,
            bucket=call.bucket,
            date_format=call.date_format,
            mask_key=call.mask_key,
            namespace=call.namespace,
            index_kernel=bound.kernel,
            native_threads=bound.native_threads,
            derive_calls=call.derive_calls,
            preserved=call.preserved,
        )
    )


def bucket_perturb_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(
        BUCKET_PERTURB, SEQ_BUCKET_DISPOSITION_KEY, load_kernel, _bucket_admit, _bucket_apply
    )


def categorical_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(
        CATEGORICAL,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
        load_kernel,
        _categorical_admit,
        _categorical_apply,
    )


def hash_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(HASH, SEQ_HASH_DISPOSITION_KEY, load_kernel, _hash_admit, _hash_apply)


def fpe_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(
        FPE,
        SEQ_FPE_DISPOSITION_KEY,
        load_kernel,
        _fpe_admit,
        _fpe_apply,
        fail_closed=fpe_fail_closed_error,
        warn=_fpe_warn,
    )


__all__ = [
    "BUCKET_PERTURB",
    "CATEGORICAL",
    "FPE",
    "HASH",
    "SEQ_BUCKET_DISPOSITION_KEY",
    "SEQ_CATEGORICAL_DISPOSITION_KEY",
    "SEQ_FPE_DISPOSITION_KEY",
    "SEQ_HASH_DISPOSITION_KEY",
    "BoundFpeKernel",
    "BucketCall",
    "CategoricalCall",
    "FpeCall",
    "HashCall",
    "NativeOp",
    "NativeResult",
    "bucket_perturb_op",
    "categorical_op",
    "fpe_op",
    "hash_op",
]
