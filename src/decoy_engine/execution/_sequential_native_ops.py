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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine import reasons
from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.execution.native._bucket_perturb_ext import native_bucket_perturb
from decoy_engine.execution.native._categorical_ext import native_categorical
from decoy_engine.execution.native._crypto_ext import FpeConfig, FpeConfigError
from decoy_engine.execution.native._date_shift_ext import FORMAT_ERROR_REASON
from decoy_engine.execution.native._fpe_route import fpe_fail_closed_error, fpe_residual_warnings
from decoy_engine.execution.native._kernels_scalar import native_text_redact
from decoy_engine.execution.native._operator_config_rejections import (
    date_shift_config_rejection,
    text_redact_config_rejection,
)
from decoy_engine.execution.native._operator_step import (
    sample_faker_array,
    sample_faker_array_positional,
)
from decoy_engine.execution.native._real_type_admission import (
    C1_PROVIDER_ALLOWLIST,
    string_source_type_rejection,
)
from decoy_engine.execution.native._requirements import faker_pool_precondition_met_seed
from decoy_engine.execution.native._text_redact_kernel import load_text_redact_kernel
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
    from decoy_engine.generation.pool import ValuePool
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.plan._types import ColumnSeed

HASH = "hash"
FPE = "fpe"
CATEGORICAL = "categorical"
BUCKET_PERTURB = "bucket_perturb"
DATE_SHIFT = "date_shift"
FAKER = "faker"
TEXT_REDACT = "text_redact"
TEXT_MASK = "text_mask"
SEQ_HASH_DISPOSITION_KEY = "sequential_hash"
SEQ_FPE_DISPOSITION_KEY = "sequential_fpe"
SEQ_CATEGORICAL_DISPOSITION_KEY = "sequential_categorical"
SEQ_BUCKET_DISPOSITION_KEY = "sequential_bucket_perturb"
SEQ_DATE_SHIFT_DISPOSITION_KEY = "sequential_date_shift"
SEQ_FAKER_DISPOSITION_KEY = "sequential_faker"
SEQ_TEXT_REDACT_DISPOSITION_KEY = "sequential_text_redact"
SEQ_TEXT_MASK_DISPOSITION_KEY = "sequential_text_mask"
FAKER_DETERMINISTIC = "deterministic"
FAKER_POSITIONAL = "positional"


@dataclass(frozen=True)
class NativeResult:
    """A kernel call's output and its per-row errors (empty for a kernel with no error channel)."""

    values: pa.Array
    errors: tuple[Any, ...] = ()
    # Per-detector counts a kernel tallies while masking (text_mask sub-floor spans), so the seam
    # can build the handler's aggregate warning without masking the column again.
    notices: Mapping[str, int] | None = None


@dataclass(frozen=True)
class NativeOp:
    strategy: str
    disposition_key: str
    load_kernel: Callable[[int | None], Any]
    admit: Callable[[str, pa.DataType, ColumnSeed], str | None]
    # A key-eligible op only: the admission used instead of `admit` for a column that is an FK
    # parent or child key, where the materialized frame is known to keep the type exact.
    apply: Callable[[Any, pa.Array, Any], NativeResult]
    fail_closed: Callable[[tuple[Any, ...], str], Exception] | None = None
    warn: Callable[[list[Any], Any, str], tuple[QualityWarning, ...]] | None = None
    # A key-eligible op only: why REMAP-minted orphan keys of this Arrow type cannot take the
    # kernel (they are typed by what they are, not by the parent column's pinned type).
    remap_input_reason: Callable[[str, pa.DataType], str | None] | None = None
    # A key-eligible op only: the admission used instead of `admit` for a column that is an FK
    # parent or child key, where the materialized frame keeps the source type exact.
    key_admit: Callable[[str, pa.DataType, ColumnSeed], str | None] | None = None


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
    preserved: np.ndarray[Any, Any] | None
    derive_calls: list[int]


@dataclass(frozen=True)
class FakerCall:
    """One faker column's call: the pool the handler already built (it carries the identity),
    the leg, and the keying that leg uses. The registry stays out; only the built pool travels.

    `deterministic` keys on the source value under `mask_key` and the configured namespace.
    `positional` keys on the row's full-table position under `job_seed` and the resolved
    selection namespace, so `row_offset` and `gate_positions` ride along."""

    leg: str
    pool: ValuePool
    namespace: str
    key: bytes
    row_offset: int = 0
    gate_positions: np.ndarray[Any, Any] | None = None


@dataclass(frozen=True)
class TextRedactCall:
    """One text_redact column's call: the handler's RESOLVED config, transported unchanged.

    `detectors=None` means every detector (the handler resolves an empty or absent selection to
    None), while the kernel reads `()` as ZERO detectors, so None must never be rewritten to
    `()` on the way through. No kernel travels: `native_text_redact` loads its own."""

    detectors: tuple[str, ...] | None
    token: str
    label_token: bool


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


def _fpe_config_reason(column: str, seed: ColumnSeed) -> str | None:
    # Gate order is part of the contract: checksum, namespace, config, then the source type.
    cfg = provider_config_to_dict(seed.provider_config)
    if cfg.get("checksum"):
        return f"fpe_checksum_not_native:{column}"
    if seed.namespace is None:
        return f"fpe_requires_namespace_stays_python:{column}"
    try:
        FpeConfig.from_mapping(cfg)._resolve()
    except FpeConfigError:
        return f"fpe_config_stays_python:{column}"
    return None


def _fpe_admit(column: str, input_type: pa.DataType, seed: ColumnSeed) -> str | None:
    """Why an fpe column stays on Python, or None.

    A truthy `checksum` is rejected before anything else, as the chunked admission does: the
    compiled FF1 kernel has no checksum path. A config the handler would raise on (no namespace,
    a duplicate or degenerate charset) also stays Python so the handler raises it as before.
    The type check is exact `string`: `large_string` declines."""
    reason = _fpe_config_reason(column, seed)
    if reason is None and input_type != pa.string():
        return f"fpe_source_type_not_string:{column}:{input_type}"
    return reason


def _fpe_key_admit(column: str, input_type: pa.DataType, seed: ColumnSeed) -> str | None:
    """`_fpe_admit` for an FK key column, which also admits a fixed-width integer source.

    Only a key qualifies: `to_pandas_fk_safe` keeps a key's integers exact, so the native cast
    and the Python handler's `str(v)` read the same value. An ordinary nullable integer payload
    is widened to float64 and stays on Python."""
    reason = _fpe_config_reason(column, seed)
    if reason is None and input_type != pa.string() and not pa.types.is_integer(input_type):
        return f"fpe_source_type_not_string:{column}:{input_type}"
    return reason


def _fpe_remap_input_reason(column: str, input_type: pa.DataType) -> str | None:
    # The parent's config was admitted at table prep; only the orphan keys' own type is open.
    if input_type != pa.string() and not pa.types.is_integer(input_type):
        return f"fpe_source_type_not_string:{column}:{input_type}"
    return None


def integer_key_representation_lost(strategy: str, pinned: pa.DataType, dtype: Any) -> bool:
    """True when an integer fpe key no longer sits in an integer pandas column.

    Checked before the coercive pin, which would turn a float-widened `[123456.0, None]` into
    exact ints and stringify it as `"123456"` where the Python handler writes `"123456.0"`."""
    return (
        strategy == FPE and pa.types.is_integer(pinned) and not pd.api.types.is_integer_dtype(dtype)
    )


def _fpe_apply(bound: BoundFpeKernel, values: pa.Array, call: FpeCall) -> NativeResult:
    if pa.types.is_integer(values.type):
        # Same text the handler's `str(v)` feeds the cipher; the output stays a string column.
        values = pc.cast(values, pa.string())
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
            code=reasons.runtime.SEQUENTIAL_FPE_NATIVE_RESULT_INVALID,
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


def _date_shift_admit(column: str, input_type: pa.DataType, seed: ColumnSeed) -> str | None:
    """Why a date_shift column stays on Python, or None.

    Adapts the route-neutral `date_shift_config_rejection` to the SEQ admit signature: a
    one-field empty table stands in for the resident source so the type gate resolves the
    pinned Arrow type, and a config the handler would raise on declines so the handler raises
    its own coded error on Python."""
    return date_shift_config_rejection(
        column,
        column,
        None,
        namespace=seed.namespace,
        provider_config=provider_config_to_dict(seed.provider_config),
        resident_sources={column: pa.schema([pa.field(column, input_type)]).empty_table()},
    )


def _faker_admit(column: str, input_type: pa.DataType, seed: ColumnSeed) -> str | None:
    """Why a faker column stays on Python, or None.

    Composed from the predicates the chunked route already gates on: the provider allowlist,
    the ColumnSeed-level deterministic-REUSE precondition, and the exact-`string` source. The
    non-deterministic leg is the REUSE mode only; UNIQUE, MATCH and SCALE need whole-column
    RNG state. A deterministic config the oracle rejects (no namespace) also declines, so the
    handler raises its own coded error. The built pool's output type is a separate check, made
    by the seam once the pool exists."""
    if seed.provider not in C1_PROVIDER_ALLOWLIST:
        return f"faker_provider_not_native:{column}:{seed.provider}"
    if seed.deterministic:
        if not faker_pool_precondition_met_seed(seed):
            return f"faker_not_deterministic_reuse_variant:{column}"
    elif seed.cardinality_mode != "reuse":
        return f"faker_nondeterministic_mode_stays_python:{column}:{seed.cardinality_mode}"
    return string_source_type_rejection(FAKER, column, pa.schema([pa.field(column, input_type)]))


def _faker_apply(bound: BoundIndexKernel, values: pa.Array, call: FakerCall) -> NativeResult:
    if call.leg == FAKER_DETERMINISTIC:
        return NativeResult(
            sample_faker_array(
                values,
                pool=call.pool,
                namespace=call.namespace,
                mask_key=call.key,
                index_kernel=bound.kernel,
                native_threads=bound.native_threads,
            )
        )
    return NativeResult(
        sample_faker_array_positional(
            values,
            pool=call.pool,
            row_offset=call.row_offset,
            job_seed=call.key,
            namespace=call.namespace,
            index_kernel=bound.kernel,
            native_threads=bound.native_threads,
            missing_mask=None,
            gate_positions=call.gate_positions,
        )
    )


def _load_text_redact(native_threads: int | None) -> Any:
    """Companion-present gate for text_redact. `load_text_redact_kernel` takes no thread budget
    and returns None (never raises) when the companion is absent, which `_admitted` reads as
    "stay Python"."""
    return load_text_redact_kernel()


def _text_redact_admit(column: str, input_type: pa.DataType, seed: ColumnSeed) -> str | None:
    """Why a text_redact column stays on Python, or None: the route-neutral config predicate
    (ner, non-string token, malformed detectors), then the exact-`string` source."""
    return text_redact_config_rejection(
        column, provider_config_to_dict(seed.provider_config)
    ) or string_source_type_rejection(
        TEXT_REDACT, column, pa.schema([pa.field(column, input_type)])
    )


def _text_redact_apply(bound: Any, values: pa.Array, call: TextRedactCall) -> NativeResult:
    return NativeResult(
        native_text_redact(
            values, detectors=call.detectors, token=call.token, label_token=call.label_token
        )
    )


def text_redact_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(
        TEXT_REDACT,
        SEQ_TEXT_REDACT_DISPOSITION_KEY,
        load_kernel,
        _text_redact_admit,
        _text_redact_apply,
    )


def date_shift_row_errors(column: str, positions: tuple[int, ...]) -> list[RowError]:
    """One `format_error` RowError per unparseable cell, as the Python handler records them."""
    return [
        RowError(
            column=column, row_index=int(pos), trigger="format_error", reason=FORMAT_ERROR_REASON
        )
        for pos in positions
    ]


def _date_shift_apply_unused(bound: Any, values: pa.Array, call: Any) -> NativeResult:
    # date_shift's kernel returns (values, format_error_positions), not a NativeResult, so
    # the seam calls it directly; reaching this slot means the routing was bypassed.
    raise AssertionError("date_shift uses date_shift_column, not apply")


def date_shift_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(
        DATE_SHIFT,
        SEQ_DATE_SHIFT_DISPOSITION_KEY,
        load_kernel,
        _date_shift_admit,
        _date_shift_apply_unused,
    )


def bucket_perturb_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(
        BUCKET_PERTURB, SEQ_BUCKET_DISPOSITION_KEY, load_kernel, _bucket_admit, _bucket_apply
    )


def faker_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(FAKER, SEQ_FAKER_DISPOSITION_KEY, load_kernel, _faker_admit, _faker_apply)


def categorical_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(
        CATEGORICAL,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
        load_kernel,
        _categorical_admit,
        _categorical_apply,
    )


def hash_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(
        HASH,
        SEQ_HASH_DISPOSITION_KEY,
        load_kernel,
        _hash_admit,
        _hash_apply,
        remap_input_reason=decline_reason,
    )


def fpe_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(
        FPE,
        SEQ_FPE_DISPOSITION_KEY,
        load_kernel,
        _fpe_admit,
        _fpe_apply,
        key_admit=_fpe_key_admit,
        fail_closed=fpe_fail_closed_error,
        warn=_fpe_warn,
        remap_input_reason=_fpe_remap_input_reason,
    )


__all__ = [
    "BUCKET_PERTURB",
    "CATEGORICAL",
    "DATE_SHIFT",
    "FAKER",
    "FAKER_DETERMINISTIC",
    "FAKER_POSITIONAL",
    "FPE",
    "HASH",
    "SEQ_BUCKET_DISPOSITION_KEY",
    "SEQ_CATEGORICAL_DISPOSITION_KEY",
    "SEQ_DATE_SHIFT_DISPOSITION_KEY",
    "SEQ_FAKER_DISPOSITION_KEY",
    "SEQ_FPE_DISPOSITION_KEY",
    "SEQ_HASH_DISPOSITION_KEY",
    "SEQ_TEXT_MASK_DISPOSITION_KEY",
    "SEQ_TEXT_REDACT_DISPOSITION_KEY",
    "TEXT_MASK",
    "TEXT_REDACT",
    "BoundFpeKernel",
    "BucketCall",
    "CategoricalCall",
    "FakerCall",
    "FpeCall",
    "HashCall",
    "NativeOp",
    "NativeResult",
    "TextRedactCall",
    "bucket_perturb_op",
    "categorical_op",
    "date_shift_op",
    "date_shift_row_errors",
    "faker_op",
    "fpe_op",
    "hash_op",
    "text_redact_op",
]
