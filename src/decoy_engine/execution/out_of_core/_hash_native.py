"""Native routing for the out-of-core `hash` mask: eligibility and the kernel call.

The out-of-core route hashes in Python (`kernel/_scalar.hash_array`). For columns
whose Arrow type the compiled companion canonicalizes identically, `mask_batch`
calls the SAME keyed-derivation kernel the native-chunked route uses
(`derive_batch`: HKDF-SHA256 then HMAC-SHA256, see `determinism/_derive.py`), so
no keyed derivation is re-implemented here.

Parity-eligible input types are the compiled kernel's admitted set minus
timezone-aware timestamps. A tz-aware column can hold an Arrow-valid tick that
pandas reads back as `NaT`: the Python path then reports a missing value where
the kernel emits a token, a null-versus-value divergence that is not the accepted
Unicode-version difference. Every other admitted type (utf8, large_utf8, bool,
the eight integer widths) matches the Python path byte for byte on text both
Unicode versions agree on; see the D1a plan, section 3.

Reason codes carry the strategy outcome, the column name and the Arrow type only,
never a cell, key or derivation value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.native._requirements import is_admitted_native_hash_type

if TYPE_CHECKING:
    from decoy_engine.execution.native._crypto_ext import KeyedDerivationKernel

REASON_UNAVAILABLE = "crypto_extension_unavailable"
REASON_TZ_TIMESTAMP = "hash_input_type_tz_timestamp_deferred"
REASON_NOT_ADMITTED = "hash_input_type_not_native_admitted"


@dataclass(frozen=True)
class BoundHashKernel:
    """The companion kernel, loaded once for the run, and the thread budget it runs with.

    `native_threads=None` leaves the budget to the compiled kernel's own default."""

    kernel: KeyedDerivationKernel
    native_threads: int | None = None


def decline_reason(column: str, arrow_type: pa.DataType) -> str | None:
    """Why `column` must stay on the Python hash, or None when it is parity-eligible."""
    if pa.types.is_timestamp(arrow_type) and arrow_type.tz is not None:
        return f"{REASON_TZ_TIMESTAMP}:{column}"
    if pa.types.is_timestamp(arrow_type) or not is_admitted_native_hash_type(arrow_type):
        return f"{REASON_NOT_ADMITTED}:{column}:{arrow_type}"
    return None


def native_hash_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    bound: BoundHashKernel,
    mask_key: bytes,
    namespace: str,
    truncate: int | None,
) -> pa.Array:
    """Hash one column slice with the compiled kernel.

    There is no fallback: a kernel fault propagates and aborts the run, so a column
    never ends up half native and half Python. The result is checked for the shape
    the Python path emits (a string array, one token per row) before the caller
    inserts it.
    """
    out = bound.kernel.derive_batch(
        values,
        mask_key=mask_key,
        namespace=namespace,
        truncate=truncate,
        native_threads=bound.native_threads,
    )
    if not isinstance(out, pa.Array) or out.type != pa.string() or len(out) != len(values):
        raise ExecutionError(
            code="out_of_core_hash_native_result_invalid",
            message="the native hash kernel returned an array that is not one string per input row.",
        )
    return out


__all__ = [
    "REASON_NOT_ADMITTED",
    "REASON_TZ_TIMESTAMP",
    "REASON_UNAVAILABLE",
    "BoundHashKernel",
    "decline_reason",
    "native_hash_array",
]
