"""Task 4.4 C2: direct native operator dispatch for the four slice
strategies.

Calls the low-level native kernels DIRECTLY -- never `run_native_or_oracle_
chunked` (`native/_dispatch.py:431`), which does whole-table admission and
silently falls back to the pandas oracle when that admission fails. Going
through it here would make the shadow's parity proof vacuous: a hash node
that could not run natively would silently mask through the oracle instead
of failing, and the comparison would "pass" against itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import pyarrow as pa

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
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

__all__ = ["OperatorCallEvidence", "run_operator"]

_PASSTHROUGH: Final = "native_passthrough"
_REDACT: Final = "native_redact"
_TRUNCATE: Final = "native_truncate"
_KEYED_HASH: Final = "native_keyed_hash"


@dataclass
class OperatorCallEvidence:
    """Per-node route evidence the coordinator accumulates across a table's
    batches (design doc section 7's route-evidence record, scoped to what
    4.4 shadows). `compiled_kernel_executed` is set ONLY after
    `native_keyed_hash` itself returns successfully -- the same positive-
    evidence contract `_chunk_masking._mask_chunk_native` uses for its own
    `NativeRouteEvidence.compiled_kernel_executed` flag -- so a hash node's
    "the Rust kernel really ran" claim is never inferred, only observed.
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
) -> pa.Array:
    """Dispatch one batch to `binding`'s bound operator, directly. Raises a
    coded `ShadowDifference(native_companion_unavailable)` -- never falls
    back to the oracle -- when the compiled hash companion is missing or
    ABI-incompatible (C2/C4).
    """
    cfg = dict(binding.resolved_config)
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
    else:  # pragma: no cover - C0 only ever binds the four slice operators
        raise AssertionError(f"unbound operator id {binding.operator_id!r}")
    evidence.actual_operator = binding.operator_id
    evidence.executed = True
    evidence.batches_run += 1
    return out
