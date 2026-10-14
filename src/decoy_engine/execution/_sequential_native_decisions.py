"""Decision and evidence value types for the SEQ-native seam.

`ColumnDecision` and `RemapRecord` record which backend a column or REMAP edge took and render
the disposition entries; they hold no kernel or pandas logic. They live apart from
`_sequential_native.py` so that module stays under its size goal and the types can be imported
without the orchestration class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution._sequential_native_ops import (
    BUCKET_PERTURB,
    DATE_SHIFT,
    FAKER,
    HASH,
    TEXT_REDACT,
)

PATH_SCALAR = "scalar"
PATH_REMAP = "fk_remap"
MIXED = "mixed"
_NO_KERNEL_RAN = "none"
_REASON_NO_ROWS = "no_rows_hashed"
_NO_CALLS_REASON = {
    BUCKET_PERTURB: "no_native_bucket_calls",
    DATE_SHIFT: "no_native_date_shift_calls",
}


@dataclass
class ColumnDecision:
    """The backend one native-capable column takes for the whole run, and how many rows it ran.

    `pinned_type` is the Arrow type of the source column, read once at table prep."""

    table: str
    column: str
    backend: str
    reason: str | None
    pinned_type: pa.DataType
    rows: int = 0
    strategy: str = HASH
    # bucket_perturb and date_shift: evidence comes from the compiled calls the kernel reported,
    # so a column that parsed nothing never claims a kernel ran; `counts_preserved` is set once
    # the native path has recorded its sentinel count.
    native_calls: int = 0
    counts_preserved: bool = False
    preserved_rows: int = 0

    @property
    def native(self) -> bool:
        return self.backend == RUST_COMPANION

    def entry(self) -> dict[str, Any]:
        calls_decide = self.strategy in (BUCKET_PERTURB, DATE_SHIFT) and self.native
        ran = self.native_calls > 0 if calls_decide else self.rows > 0
        none_reason = (
            _NO_CALLS_REASON.get(self.strategy, _REASON_NO_ROWS) if self.rows else _REASON_NO_ROWS
        )
        reason = self.reason or (None if ran else none_reason)
        entry: dict[str, Any] = {
            "table": self.table,
            "column": self.column,
            "path": PATH_SCALAR,
            "executed_backend": self.backend if ran else _NO_KERNEL_RAN,
        }
        if reason is not None:
            entry["reason"] = reason
        if self.strategy in (FAKER, TEXT_REDACT):
            # The numeric count is the evidence that distinguishes an empty table (no call)
            # from an all-null one (one call), which `executed_backend` alone cannot.
            entry["native_calls"] = self.native_calls
        if self.counts_preserved:
            entry["preserved_rows"] = self.preserved_rows
        return entry


@dataclass
class RemapRecord:
    """Orphan rows minted by re-running one parent column's key strategy, by backend."""

    table: str
    column: str
    native_rows: int = 0
    python_rows: int = 0
    reasons: list[str] = field(default_factory=list)
    strategy: str = HASH

    def entry(self) -> dict[str, Any]:
        if self.native_rows and self.python_rows:
            backend = MIXED
        elif self.native_rows:
            backend = RUST_COMPANION
        else:
            backend = PYTHON_SCALAR
        entry: dict[str, Any] = {
            "table": self.table,
            "column": self.column,
            "path": PATH_REMAP,
            "executed_backend": backend,
        }
        if self.reasons:
            entry["reason"] = self.reasons[0]
        return entry
