"""Native array-to-array kernels for the non-keyed row-local strategies.

`passthrough`, `redact`, and `truncate` need no Rust: they are pure per-value
transforms over one Arrow array with no cross-row state and no keyed draw, so
lowering them means calling the existing `kernel/_scalar.py` functions
directly instead of round-tripping through a pandas column and `to_pylist()`
the way `_strategies/_redact.py` and `_strategies/_truncate.py` do. Reusing
those functions (rather than re-expressing their per-value logic here) is
what makes native output byte-identical to the shipped handlers: there is one
logic source, and this module is just its Arrow-native entry point.

`truncate`'s fail-closed config validation lives in `TruncateHandler.run`,
not in `kernel.truncate_array` (the kernel trusts its caller). A native
caller bypasses the handler entirely, so this module re-raises the same
`StrategyError` codes (`truncate_length_invalid` / `truncate_keep_invalid` /
`truncate_mask_char_invalid`) the handler raises, ahead of calling the
kernel, so an invalid config still fails closed instead of running with a
meaningless length/keep/mask_char triple.

Output type: these kernels emit each strategy's natural, per-batch-stable Arrow
type -- `pa.string()` for truncate and for the admitted string-redact contract,
across value-bearing, all-null, and empty batches alike, so the out-of-core writer
can concatenate a column's batches under one schema. `truncate_array` already forces
`pa.string()`; `redact_array` infers (null for all-null/empty), so `native_redact`
pins the string case here.

The pandas oracle's `pa.Table.from_pandas` round-trip infers a DIFFERENT type in three
cases. Two are degenerate but realistic streaming batch shapes: an all-null column
becomes null-type, and an empty (zero-row) column becomes double (pandas' empty
default). There native emits the stable string a streaming column needs and the
oracle's type is the pandas inference artifact -- reconciling it is a route-integration
concern (Task 2.6/2.7). The third is genuinely out of the admitted contract: a
non-string `redact_with` (no shipped disguise uses one) stays inferred, so native keeps
the value's own type (int+null -> int64) while pandas promotes to double; eligibility
excludes it before production routing. See `tests/native/test_kernels_scalar.py` for the
pinned divergences and the batch-schema-stability test.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.execution._errors import StrategyError
from decoy_engine.kernel import passthrough_array, redact_array, truncate_array


def native_passthrough(array: pa.Array | pa.ChunkedArray) -> pa.Array:
    """Return `array` unchanged (the passthrough strategy is a no-op)."""
    return passthrough_array(array)


def native_redact(
    array: pa.Array | pa.ChunkedArray | list[Any],
    *,
    redact_with: Any = "REDACTED",
) -> pa.Array:
    """Replace every non-null value in `array` with `redact_with`; nulls stay null.

    `redact_array` lets Arrow infer the output type, which is null for an all-null or
    empty batch and string only once a value is present. That per-batch instability
    would break the out-of-core writer, which concatenates a column's batches under one
    schema. For the admitted string-redact contract (every shipped disguise), pin the
    output to `pa.string()` so every batch of a redact column carries the same type,
    matching `native_truncate` and the native keyed kernel. A non-string `redact_with`
    is outside the admitted set (enforced at eligibility); its inferred type is left
    untouched and characterized in the tests.
    """
    result = redact_array(array, redact_with=redact_with)
    if isinstance(redact_with, str) and result.type != pa.string():
        return result.cast(pa.string())
    return result


def native_truncate(
    array: pa.Array | pa.ChunkedArray | list[Any],
    *,
    length: int,
    keep: str = "head",
    mask_char: str | None = None,
) -> pa.Array:
    """Truncate `array` to `length` chars, keeping the head or tail; nulls stay null.

    Validation order matches `TruncateHandler.run` exactly (length, then
    keep, then mask_char) so the first violation in a doubly-invalid config
    raises the same code from both paths.
    """
    if not isinstance(length, int) or length < 1:
        raise StrategyError(
            code="truncate_length_invalid",
            strategy="truncate",
            message=(
                f"truncate requires an integer length >= 1, got {length!r} "
                f"({type(length).__name__})."
            ),
        )
    if keep not in ("head", "tail"):
        raise StrategyError(
            code="truncate_keep_invalid",
            strategy="truncate",
            message=f"truncate requires keep in ('head', 'tail'), got {keep!r}.",
        )
    if mask_char is not None and (not isinstance(mask_char, str) or len(mask_char) != 1):
        raise StrategyError(
            code="truncate_mask_char_invalid",
            strategy="truncate",
            message=(
                f"truncate requires mask_char to be a single character, got "
                f"{mask_char!r} ({type(mask_char).__name__})."
            ),
        )
    return truncate_array(array, length=length, keep=keep, mask_char=mask_char)


__all__ = ["native_passthrough", "native_redact", "native_truncate"]
