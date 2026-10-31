"""Native routing for the out-of-core `text_redact` mask.

The out-of-core route redacts text per cell in Python (`_mask_group_b.text_redact_array`, which
stays the reference and the declined path). For columns the compiled span kernel handles
identically, the Group (b) adapter calls the SAME `native_text_redact` the sequential and
native-chunked routes call, so no detector, splice or replay logic is re-implemented here.

Admission is the sequential route's predicate: `text_redact_config_rejection` (a truthy `ner`, a
non-string `token`, malformed `detectors`) plus a source that is exactly `pa.string()`. The
decision is made once per table before the first batch and is immutable for the run; a native
column is held to its pinned source type on every batch and aborts the run on drift.

The resolved `detectors` is transported verbatim. `None` means every detector (an absent, null or
empty selection all resolve to it); the kernel reads `()` as zero detectors, so substituting it
would leave PII unredacted.

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
from decoy_engine.execution._strategies._text_redact import _DEFAULT_TOKEN
from decoy_engine.execution.native._kernels_scalar import native_text_redact
from decoy_engine.execution.native._operator_config_rejections import (
    text_redact_config_rejection,
)
from decoy_engine.execution.native._real_type_admission import string_source_type_rejection
from decoy_engine.execution.out_of_core._mask_group_b import _text_redact_detector_ids

if TYPE_CHECKING:
    from decoy_engine.plan._types import ColumnSeed

REASON_UNAVAILABLE = reasons.route.TEXT_REDACT_EXTENSION_UNAVAILABLE
_NO_BACKEND_RAN = "none"
_REASON_NO_NATIVE_CALLS = reasons.route.NO_NATIVE_KERNEL_CALLS


@dataclass(frozen=True)
class TextRedactConfig:
    """The column's config as the kernel takes it, resolved once for the run."""

    detectors: tuple[str, ...] | None
    token: str
    label_token: bool


def decline_reason(
    column: str, schema: pa.Schema, seed: ColumnSeed
) -> tuple[TextRedactConfig | None, str | None]:
    """`(config, None)` when `column` can run on the compiled kernel, else `(None, reason)`."""
    cfg = provider_config_to_dict(seed.provider_config)
    rejection = text_redact_config_rejection(column, cfg) or string_source_type_rejection(
        "text_redact", column, schema
    )
    if rejection is not None:
        return None, rejection
    ids, ok = _text_redact_detector_ids(cfg.get("detectors"))
    if not ok:  # pragma: no cover - the config predicate already declined a malformed selection
        return None, f"text_redact_detectors_malformed:{column}"
    return (
        TextRedactConfig(
            None if ids is None else tuple(ids),
            cfg.get("token", _DEFAULT_TOKEN),
            bool(cfg.get("label_token", False)),
        ),
        None,
    )


@dataclass(frozen=True)
class BoundTextRedactKernel:
    """Marks the companion as present for the run.

    `native_text_redact` loads the span kernel itself, so this holds the loader's answer only
    as the gate. Defined here, not imported from the sequential route, to avoid a circular
    import."""

    kernel: Any


@dataclass
class TextRedactColumnDecision:
    """The backend one text_redact column takes for the whole run, and what it executed.

    `pinned_type` is the source Arrow type a native column is held to. `native_calls` counts
    the kernel invocations over the WHOLE column, so the disposition reports a native run only
    when the kernel was actually called."""

    table: str
    column: str
    backend: str
    reason: str | None
    pinned_type: pa.DataType
    config: TextRedactConfig | None = None
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
class NativeTextRedact:
    """What `mask_column` needs to run one text_redact column on the compiled kernel."""

    bound: BoundTextRedactKernel
    decision: TextRedactColumnDecision


class TableTextRedactPlan:
    """The immutable per-column decisions for one table, and the per-batch gate."""

    def __init__(
        self,
        decisions: Mapping[str, TextRedactColumnDecision],
        bound: BoundTextRedactKernel | None,
    ) -> None:
        self._decisions = dict(decisions)
        self._bound = bound

    @property
    def decisions(self) -> Mapping[str, TextRedactColumnDecision]:
        return self._decisions

    def gate(self, column: str, batch: pa.RecordBatch) -> NativeTextRedact | None:
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
                    code=reasons.runtime.OUT_OF_CORE_TEXT_REDACT_COLUMN_MISSING,
                    message=(
                        f"text_redact column {column!r} of table {decision.table!r} is missing "
                        "from a streamed batch; the run is aborted rather than skipping it."
                    ),
                )
            if batch.schema.field(idx).type != decision.pinned_type:
                raise ExecutionError(
                    code=reasons.runtime.OUT_OF_CORE_TEXT_REDACT_SOURCE_TYPE_DRIFT,
                    message=(
                        f"text_redact column {column!r} of table {decision.table!r} was pinned "
                        f"as {decision.pinned_type} but a batch carries "
                        f"{batch.schema.field(idx).type}; the run is aborted rather than "
                        "changing path mid-column."
                    ),
                )
        if idx >= 0:
            decision.rows += batch.num_rows
        if decision.native and self._bound is not None:
            return NativeTextRedact(self._bound, decision)
        return None


def decide_table(
    table_name: str,
    schema: pa.Schema,
    seeds: Mapping[str, ColumnSeed],
    bound: BoundTextRedactKernel | None,
) -> TableTextRedactPlan:
    """Decide every text_redact column of one table, before its first batch is read."""
    decisions: dict[str, TextRedactColumnDecision] = {}
    for column, seed in seeds.items():
        config: TextRedactConfig | None = None
        reason: str | None = REASON_UNAVAILABLE
        if bound is not None:
            config, reason = decline_reason(column, schema, seed)
        decisions[column] = TextRedactColumnDecision(
            table_name,
            column,
            RUST_COMPANION if config is not None else PYTHON_SCALAR,
            reason,
            schema.field(column).type,
            config,
        )
    return TableTextRedactPlan(decisions, bound)


def native_text_redact_array(
    values: pa.Array | pa.ChunkedArray, *, native: NativeTextRedact
) -> pa.Array:
    """Redact one column slice with the compiled kernel.

    There is no fallback and no catch: a kernel fault propagates and aborts the run, because
    continuing on Python would be silent for the rows already emitted and a swallowed fault would
    pass unredacted text through. text_redact has no per-row error channel, so the only check on
    the result is its shape."""
    config = native.decision.config
    if config is None:  # pragma: no cover - a native decision always carries its config
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_TEXT_REDACT_UNPREPARED,
            message="a native text_redact column reached the kernel without a resolved config.",
        )
    native.decision.native_calls += 1
    out = native_text_redact(
        values,
        detectors=config.detectors,
        token=config.token,
        label_token=config.label_token,
    )
    if not isinstance(out, pa.Array) or out.type != pa.string() or len(out) != len(values):
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_TEXT_REDACT_NATIVE_RESULT_INVALID,
            message=(
                "the native text_redact kernel returned an array that is not one string per "
                "input row."
            ),
        )
    return out


__all__ = [
    "REASON_UNAVAILABLE",
    "BoundTextRedactKernel",
    "NativeTextRedact",
    "TableTextRedactPlan",
    "TextRedactColumnDecision",
    "TextRedactConfig",
    "decide_table",
    "decline_reason",
    "native_text_redact_array",
]
