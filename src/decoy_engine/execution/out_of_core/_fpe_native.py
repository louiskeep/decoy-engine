"""Native routing for the out-of-core `fpe` mask.

The out-of-core route encrypts fpe columns in a per-cell Python loop
(`_mask_group_b.fpe_array`, which stays the reference and the declined path). For columns the
compiled FF1 kernel handles identically, the Group (b) adapter calls the SAME kernel the
sequential and native-chunked routes call, so no cipher, tweak or charset logic is
re-implemented here.

Admission is the sequential route's `_fpe_admit` predicate (checksum, missing namespace, an
`FpeConfig` that fails to resolve) plus a source that is exactly `pa.string()`. The decision is
made once per table before the first batch and is immutable for the run; a native column is
held to its pinned source type on every batch and aborts the run on drift.

Reason codes carry the strategy outcome, the column name and the Arrow type only, never a
cell, key or charset.
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
from decoy_engine.execution.native._crypto_ext import FpeConfig, FpeConfigError
from decoy_engine.execution.native._fpe_route import fpe_fail_closed_error

if TYPE_CHECKING:
    from decoy_engine.plan._types import ColumnSeed

REASON_UNAVAILABLE = reasons.route.FPE_EXTENSION_UNAVAILABLE
_NO_BACKEND_RAN = "none"
_REASON_NO_NATIVE_CALLS = reasons.route.NO_NATIVE_KERNEL_CALLS


def decline_reason(
    column: str, schema: pa.Schema, seed: ColumnSeed
) -> tuple[FpeConfig | None, str | None]:
    """`(config, None)` when `column` can run on the compiled kernel, else `(None, reason)`.

    The order matches the sequential admission: a truthy checksum first (the kernel has no
    checksum path), then the namespace, then a config the Python path would raise on, so the
    Python path raises it as before; the exact-`string` source check comes last."""
    cfg = provider_config_to_dict(seed.provider_config)
    if cfg.get("checksum"):
        return None, f"fpe_checksum_not_native:{column}"
    if seed.namespace is None:
        return None, f"fpe_requires_namespace_stays_python:{column}"
    config = FpeConfig.from_mapping(cfg)
    try:
        config._resolve()
    except FpeConfigError:
        return None, f"fpe_config_stays_python:{column}"
    column_type = schema.field(column).type
    if column_type != pa.string():
        return None, f"fpe_source_type_not_string:{column}:{column_type}"
    return config, None


@dataclass(frozen=True)
class BoundFpeKernel:
    """The compiled FF1 kernel, loaded once for the run, and the thread budget it runs with.

    Defined here, not imported from the sequential route's identical holder: that module
    imports this package, so importing it back would be circular."""

    kernel: Any
    native_threads: int | None = None


@dataclass
class FpeColumnDecision:
    """The backend one fpe column takes for the whole run, and what it executed.

    `pinned_type` is the source Arrow type a native column is held to. `native_calls` counts
    the kernel invocations over the WHOLE column, so the disposition reports a native run only
    when the kernel was actually called."""

    table: str
    column: str
    backend: str
    reason: str | None
    pinned_type: pa.DataType
    config: FpeConfig | None = None
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
class NativeFpe:
    """What `mask_column` needs to run one fpe column on the compiled kernel."""

    bound: BoundFpeKernel
    decision: FpeColumnDecision


class TableFpePlan:
    """The immutable per-column decisions for one table, and the per-batch gate."""

    def __init__(
        self, decisions: Mapping[str, FpeColumnDecision], bound: BoundFpeKernel | None
    ) -> None:
        self._decisions = dict(decisions)
        self._bound = bound

    @property
    def decisions(self) -> Mapping[str, FpeColumnDecision]:
        return self._decisions

    def gate(self, column: str, batch: pa.RecordBatch) -> NativeFpe | None:
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
                    code=reasons.runtime.OUT_OF_CORE_FPE_COLUMN_MISSING,
                    message=(
                        f"fpe column {column!r} of table {decision.table!r} is missing "
                        "from a streamed batch; the run is aborted rather than skipping it."
                    ),
                )
            if batch.schema.field(idx).type != decision.pinned_type:
                raise ExecutionError(
                    code=reasons.runtime.OUT_OF_CORE_FPE_SOURCE_TYPE_DRIFT,
                    message=(
                        f"fpe column {column!r} of table {decision.table!r} was pinned "
                        f"as {decision.pinned_type} but a batch carries "
                        f"{batch.schema.field(idx).type}; the run is aborted rather than "
                        "changing path mid-column."
                    ),
                )
        if idx >= 0:
            decision.rows += batch.num_rows
        if decision.native and self._bound is not None:
            return NativeFpe(self._bound, decision)
        return None


def decide_table(
    table_name: str,
    schema: pa.Schema,
    seeds: Mapping[str, ColumnSeed],
    bound: BoundFpeKernel | None,
) -> TableFpePlan:
    """Decide every fpe column of one table, before its first batch is read."""
    decisions: dict[str, FpeColumnDecision] = {}
    for column, seed in seeds.items():
        config: FpeConfig | None = None
        reason: str | None = REASON_UNAVAILABLE
        if bound is not None:
            config, reason = decline_reason(column, schema, seed)
        decisions[column] = FpeColumnDecision(
            table_name,
            column,
            RUST_COMPANION if config is not None else PYTHON_SCALAR,
            reason,
            schema.field(column).type,
            config,
        )
    return TableFpePlan(decisions, bound)


def native_fpe_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    native: NativeFpe,
    mask_key: bytes,
    namespace: str,
    column: str,
) -> pa.Array:
    """Encrypt one column slice with the compiled kernel.

    There is no fallback: a kernel fault propagates and aborts the run, so a column never ends
    up half native and half Python. A per-row failure maps to the same `StrategyError` code the
    Python loop raises (first failing row). The kernel validates the key and tweak itself and
    answers an empty or all-null slice with a `string` array, matching the Python loop's
    always-`string` output, so no slice is short-circuited here."""
    config = native.decision.config
    if config is None:  # pragma: no cover - a native decision always carries its config
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_FPE_UNPREPARED,
            message="a native fpe column reached the kernel without a resolved config.",
        )
    native.decision.native_calls += 1
    result = native.bound.kernel.run(
        values,
        mask_key=mask_key,
        namespace=namespace,
        tweak_column=column,
        config=config,
        forward=True,
        native_threads=native.bound.native_threads,
    )
    if result.errors:
        raise fpe_fail_closed_error(result.errors, column)
    out = result.values
    if not isinstance(out, pa.Array) or out.type != pa.string() or len(out) != len(values):
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_FPE_NATIVE_RESULT_INVALID,
            message="the native fpe kernel returned an array that is not one string per input row.",
        )
    return out


__all__ = [
    "REASON_UNAVAILABLE",
    "BoundFpeKernel",
    "FpeColumnDecision",
    "NativeFpe",
    "TableFpePlan",
    "decide_table",
    "decline_reason",
    "native_fpe_array",
]
