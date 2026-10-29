"""Native routing for the out-of-core `text_mask` span mask.

The out-of-core route masks PII spans per cell in Python (`_mask_group_c._text_mask_array`, which
stays the reference and the declined path). For columns the compiled span kernel handles
identically, this adapter calls the SAME `native_text_mask` the sequential and native-chunked
routes call, so detection comes from the compiled `text_redact` span kernel and no detector,
splice, fpe/faker/date_shift span, or sub-floor logic is re-implemented here. This is a compute
change only: `text_mask` is already out-of-core admitted, so route admission does not change.

Admission is the sequential route's predicate: `text_mask_config_rejection` (a truthy `ner`, the
one branch that loads a spaCy model and supplies extra spans the compiled path does not produce)
plus a source that is exactly `pa.string()`. The decision is made once per table before the first
batch and is immutable for the run; a native column is held to its pinned source type on every
batch and aborts the run on drift.

The resolved config is transported verbatim. `detectors=None` means every detector (an absent,
null or empty selection all resolve to it); the kernel reads `()` as zero detectors, so
substituting it would leave PII unmasked. The keyed spans (fpe/faker/date_shift) take `mask_key`,
which the route resolves to the job seed, exactly as the per-cell path already passes it.

Reason codes carry the strategy outcome, the column name and the Arrow type only, never a cell.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution.native._kernels_scalar import native_text_mask
from decoy_engine.execution.native._operator_config_rejections import text_mask_config_rejection
from decoy_engine.execution.native._real_type_admission import string_source_type_rejection

if TYPE_CHECKING:
    from decoy_engine.plan._types import ColumnSeed

REASON_UNAVAILABLE = reasons.route.TEXT_MASK_EXTENSION_UNAVAILABLE
_NO_BACKEND_RAN = "none"
_REASON_NO_NATIVE_CALLS = reasons.route.NO_NATIVE_KERNEL_CALLS

_DEFAULT_TOKEN = "[REDACTED]"  # noqa: S105 - redaction placeholder, not a credential
_DEFAULT_POLICY = "redact"
_DATE_BOUND_KEYS = ("min_days", "max_days")


@dataclass(frozen=True)
class TextMaskConfig:
    """The column's resolved config as `native_text_mask` takes it, fixed once for the run.

    Resolution mirrors `_mask_group_c._text_mask_array` and `TextMaskHandler.run` exactly:
    `detectors` is `None` (every detector) or a non-empty tuple, never `()`; `per_detector` is the
    strategy map or `None`; a date bound that is PRESENT-but-null is kept (so a per-span
    date_shift crashes identically to the oracle) rather than defaulted away."""

    detectors: tuple[str, ...] | None
    per_detector: Mapping[str, str] | None
    unmatched_span_policy: str
    token: str
    date_shift_bounds: tuple[tuple[str, int | None], ...]
    sub_floor_span_policy: str | None


def _resolve_config(cfg: Mapping[str, Any]) -> TextMaskConfig:
    detectors_raw = cfg.get("detectors")
    if isinstance(detectors_raw, (list, tuple)):
        detector_ids = [str(d) for d in detectors_raw] or None
    else:
        detector_ids = None
    per_detector = dict(cfg.get("per_detector_strategy") or {})
    sub_floor_raw = cfg.get("sub_floor_span")
    return TextMaskConfig(
        detectors=tuple(detector_ids) if detector_ids is not None else None,
        per_detector=per_detector or None,
        unmatched_span_policy=str(cfg.get("unmatched_span_policy", _DEFAULT_POLICY)),
        token=str(cfg.get("token", _DEFAULT_TOKEN)),
        date_shift_bounds=tuple((key, cfg[key]) for key in _DATE_BOUND_KEYS if key in cfg),
        sub_floor_span_policy=str(sub_floor_raw) if sub_floor_raw is not None else None,
    )


def decline_reason(
    column: str, schema: pa.Schema, seed: ColumnSeed
) -> tuple[TextMaskConfig | None, str | None]:
    """`(config, None)` when `column` can run on the compiled span kernel, else `(None, reason)`."""
    cfg = provider_config_to_dict(seed.provider_config)
    rejection = text_mask_config_rejection(column, cfg) or string_source_type_rejection(
        "text_mask", column, schema
    )
    if rejection is not None:
        return None, rejection
    return _resolve_config(cfg), None


@dataclass(frozen=True)
class BoundTextMaskKernel:
    """Marks the span companion as present for the run.

    `native_text_mask` loads the `text_redact` span kernel itself (text_mask shares it), so this
    holds the loader's answer only as the gate. Defined here, not imported from the sequential
    route, to avoid a circular import."""

    kernel: Any


@dataclass
class TextMaskColumnDecision:
    """The backend one text_mask column takes for the whole run, and what it executed.

    `pinned_type` is the source Arrow type a native column is held to. `native_calls` counts the
    kernel invocations over the WHOLE column, so the disposition reports a native run only when the
    kernel was actually called."""

    table: str
    column: str
    backend: str
    reason: str | None
    pinned_type: pa.DataType
    config: TextMaskConfig | None = None
    rows: int = 0
    native_calls: int = 0

    @property
    def native(self) -> bool:
        return self.backend == RUST_COMPANION

    def entry(self) -> dict[str, Any]:
        ran = self.native_calls > 0 if self.native else self.rows > 0
        reason = self.reason or (None if ran else _REASON_NO_NATIVE_CALLS)
        entry: dict[str, Any] = {
            "table": self.table,
            "column": self.column,
            "path": "mask_batch",
            "executed_backend": self.backend if ran else _NO_BACKEND_RAN,
        }
        if reason is not None:
            entry["reason"] = reason
        return entry


@dataclass(frozen=True)
class NativeTextMask:
    """What `_text_mask_array` needs to run one text_mask column on the compiled span kernel."""

    bound: BoundTextMaskKernel
    decision: TextMaskColumnDecision


class TableTextMaskPlan:
    """The immutable per-column decisions for one table, and the per-batch gate."""

    def __init__(
        self,
        decisions: Mapping[str, TextMaskColumnDecision],
        bound: BoundTextMaskKernel | None,
    ) -> None:
        self._decisions = dict(decisions)
        self._bound = bound

    @property
    def decisions(self) -> Mapping[str, TextMaskColumnDecision]:
        return self._decisions

    def gate(self, column: str, batch: pa.RecordBatch) -> NativeTextMask | None:
        """The native handle for `column` in `batch`, or None when it stays on Python.

        A native column is held to its pinned source type: a batch that lacks it or carries
        another type aborts the run, so a column never changes path mid-stream."""
        decision = self._decisions.get(column)
        if decision is None:
            return None
        idx = batch.schema.get_field_index(column)
        if decision.native:
            if idx < 0:
                raise ExecutionError(
                    code=reasons.runtime.OUT_OF_CORE_TEXT_MASK_COLUMN_MISSING,
                    message=(
                        f"text_mask column {column!r} of table {decision.table!r} is missing "
                        "from a streamed batch; the run is aborted rather than skipping it."
                    ),
                )
            if batch.schema.field(idx).type != decision.pinned_type:
                raise ExecutionError(
                    code=reasons.runtime.OUT_OF_CORE_TEXT_MASK_SOURCE_TYPE_DRIFT,
                    message=(
                        f"text_mask column {column!r} of table {decision.table!r} was pinned "
                        f"as {decision.pinned_type} but a batch carries "
                        f"{batch.schema.field(idx).type}; the run is aborted rather than "
                        "changing path mid-column."
                    ),
                )
        if idx >= 0:
            decision.rows += batch.num_rows
        if decision.native and self._bound is not None:
            return NativeTextMask(self._bound, decision)
        return None


def decide_table(
    table_name: str,
    schema: pa.Schema,
    seeds: Mapping[str, ColumnSeed],
    bound: BoundTextMaskKernel | None,
) -> TableTextMaskPlan:
    """Decide every text_mask column of one table, before its first batch is read."""
    decisions: dict[str, TextMaskColumnDecision] = {}
    for column, seed in seeds.items():
        config: TextMaskConfig | None = None
        reason: str | None = REASON_UNAVAILABLE
        if bound is not None:
            config, reason = decline_reason(column, schema, seed)
        decisions[column] = TextMaskColumnDecision(
            table_name,
            column,
            RUST_COMPANION if config is not None else PYTHON_SCALAR,
            reason,
            schema.field(column).type,
            config,
        )
    return TableTextMaskPlan(decisions, bound)


def native_text_mask_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    native: NativeTextMask,
    mask_key: bytes,
    sub_floor_notices: dict[str, int] | None,
) -> pa.Array:
    """Span-mask one column slice with the compiled span kernel.

    `native_text_mask` reproduces `TextMaskHandler.run` per cell (compiled detection spliced with
    the unchanged Python per-span strategies), threads ONE sub-floor notice dict and raises the
    same `StrategyError(code='fpe_unencryptable_domain')` on a fail-closed span, so this route
    raises where the per-cell loop did. Its per-call notices are merged into the caller's per-table
    accumulator so the one aggregate `text_mask_sub_floor_span_handled` warning is unchanged."""
    config = native.decision.config
    if config is None:  # pragma: no cover - a native decision always carries its config
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_TEXT_MASK_UNPREPARED,
            message="a native text_mask column reached the kernel without a resolved config.",
        )
    native.decision.native_calls += 1
    out, notices = native_text_mask(
        values,
        mask_key=mask_key,
        column=native.decision.column,
        detectors=config.detectors,
        per_detector_strategy=config.per_detector,
        unmatched_span_policy=config.unmatched_span_policy,
        token=config.token,
        date_shift_bounds=config.date_shift_bounds,
        sub_floor_span_policy=config.sub_floor_span_policy,
    )
    if sub_floor_notices is not None:
        for detector_id, count in notices.items():
            sub_floor_notices[detector_id] = sub_floor_notices.get(detector_id, 0) + count
    if not isinstance(out, pa.Array) or out.type != pa.string() or len(out) != len(values):
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_TEXT_MASK_NATIVE_RESULT_INVALID,
            message=(
                "the native text_mask kernel returned an array that is not one string per "
                "input row."
            ),
        )
    return out


__all__ = [
    "REASON_UNAVAILABLE",
    "BoundTextMaskKernel",
    "NativeTextMask",
    "TableTextMaskPlan",
    "TextMaskColumnDecision",
    "TextMaskConfig",
    "decide_table",
    "decline_reason",
    "native_text_mask_array",
]
