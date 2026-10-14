"""The sequential (SEQ) native operator for `text_mask`.

Same shape as the `text_redact` operator in `_sequential_native_ops.py`, kept in its own module
so that file stays under the size policy. Detection runs through `native_text_mask`, which takes
its spans from the compiled `text_redact` span kernel (the `regex` crate; see
`execution/native/_text_redact_kernel.py`) and splices them with the unchanged Python per-span
strategies, so output is byte-identical to `TextMaskHandler`.

Two things differ from a values-in, values-out operator. The sub-floor notice COUNTS ride back on
`NativeResult.notices` so the seam builds the handler's one aggregate warning from them without
masking the column a second time. And the fail-closed `StrategyError` is raised by
`native_text_mask` itself (the same helper the unified and chunked routes use), so this operator
has no per-row error channel and no `fail_closed` hook.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._sequential_native_ops import (
    SEQ_TEXT_MASK_DISPOSITION_KEY,
    TEXT_MASK,
    NativeOp,
    NativeResult,
)
from decoy_engine.execution.native._kernels_scalar import native_text_mask
from decoy_engine.execution.native._operator_config_rejections import text_mask_config_rejection
from decoy_engine.execution.native._real_type_admission import string_source_type_rejection

if TYPE_CHECKING:
    from decoy_engine.plan._types import ColumnSeed


@dataclass(frozen=True)
class TextMaskCall:
    """One text_mask column's call: the handler's RESOLVED config, transported unchanged.

    `detectors=None` means every detector (the handler resolves an empty or absent selection to
    None) while the kernel reads `()` as ZERO detectors, so None must never be rewritten to `()`.
    """

    column: str
    mask_key: bytes
    detectors: tuple[str, ...] | None
    per_detector_strategy: Mapping[str, str] | None
    unmatched_span_policy: str
    token: str
    date_shift_bounds: tuple[tuple[str, int | None], ...]
    sub_floor_span_policy: str | None


def _text_mask_admit(column: str, input_type: pa.DataType, seed: ColumnSeed) -> str | None:
    """Why a text_mask column stays on Python, or None: the route-neutral config predicate
    (ner), then the exact-`string` source."""
    return text_mask_config_rejection(
        column, provider_config_to_dict(seed.provider_config)
    ) or string_source_type_rejection(TEXT_MASK, column, pa.schema([pa.field(column, input_type)]))


def _text_mask_apply(bound: Any, values: pa.Array, call: TextMaskCall) -> NativeResult:
    out, notices = native_text_mask(
        values,
        mask_key=call.mask_key,
        column=call.column,
        detectors=call.detectors,
        per_detector_strategy=call.per_detector_strategy,
        unmatched_span_policy=call.unmatched_span_policy,
        token=call.token,
        date_shift_bounds=call.date_shift_bounds,
        sub_floor_span_policy=call.sub_floor_span_policy,
    )
    return NativeResult(out, notices=notices)


def text_mask_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(
        TEXT_MASK,
        SEQ_TEXT_MASK_DISPOSITION_KEY,
        load_kernel,
        _text_mask_admit,
        _text_mask_apply,
    )


__all__ = [
    "SEQ_TEXT_MASK_DISPOSITION_KEY",
    "TEXT_MASK",
    "TextMaskCall",
    "text_mask_op",
]
