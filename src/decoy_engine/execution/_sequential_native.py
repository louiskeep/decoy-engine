"""Native routing for the `hash` mask on the sequential (SEQ) FK route.

`run_sequential` masks one table at a time in pandas. For a hash column whose Arrow type the
compiled companion canonicalizes identically, the hash handler asks this module for the
tokens and gets them from the SAME keyed-derivation kernel the out-of-core route calls
(`out_of_core/_hash_native.py`: `native_hash_array`, `decline_reason`, reused unchanged), so no
keyed derivation is re-implemented here. The handler keeps its pandas orchestration; only the
token computation moves.

Three FK paths stay distinct, because only one of them calls the kernel at the scalar seam:

1. Parent or payload derivation: a hash column is masked at the handler. Native when its
   input type was admitted at table prep.
2. Matched child: `resolve_fk_keys` hands the child the parent's already-masked value. No
   kernel call and no re-derivation; the disposition reports the child as inherited, with the
   parent it came from.
3. REMAP: an orphan key is minted by running the PARENT's handler again (`make_remap_fn`).
   That call is admitted on its own, from the type of the orphan keys it actually holds, and
   only when the parent column itself took the native path, so one column never mixes two
   Unicode normalizations.

Boundary with pandas. The kernel is Arrow in, Arrow out; the handler holds a pandas column.
Input is rebuilt as an Arrow array of the column's PINNED source type (read from the Arrow
source at table prep), never inferred from pandas: inference would turn `large_string` into
`string` and an all-null column into `null`, which are expected representation changes and
not drift. Output is a plain list assigned positionally, exactly what the Python handler
assigns, so a `when` selection with a gapped index lands on the right rows and an empty
selection leaves the dtype the handler leaves today. An integer column that holds a null never
reaches here: `reject_null_bearing_int` still refuses it before conversion.

There is no fallback. A kernel fault, or a column that stops converting to its pinned type,
propagates and aborts the run, so no column is ever part native and part Python and no
Python retry follows a native selection. Whether an abort leaves earlier tables behind
depends on the sink (see `_sequential.py`): a transactional sink discards them, a callable
sink has already delivered them.

Reason codes carry the strategy outcome, the column name and the Arrow type only, never a
cell, key or derived value.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd
import pyarrow as pa

from decoy_engine.execution._adapter import pandas_column_to_kernel_input
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.out_of_core._hash_native import (
    REASON_UNAVAILABLE,
    BoundHashKernel,
    decline_reason,
    native_hash_array,
)

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import StrategyContext
    from decoy_engine.plan._types import ColumnSeed, Plan, TableSeed
    from decoy_engine.relationships._graph import RelationshipGraph

SEQ_HASH_DISPOSITION_KEY = "sequential_hash"
PATH_SCALAR = "scalar"
PATH_REMAP = "fk_remap"
PATH_INHERITED = "fk_inherited"
INHERITED = "inherited"
MIXED = "mixed"
REASON_REMAP_PARENT_PYTHON = "hash_remap_parent_stays_python"
REASON_REMAP_INPUT_NOT_ARROW = "hash_remap_input_not_arrow"
_NO_KERNEL_RAN = "none"
_REASON_NO_ROWS = "no_rows_hashed"
_CONVERSION_ERRORS = (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError)


@dataclass
class ColumnDecision:
    """The backend one hash column takes for the whole run, and how many rows it hashed.

    `pinned_type` is the Arrow type of the source column, read once at table prep."""

    table: str
    column: str
    backend: str
    reason: str | None
    pinned_type: pa.DataType
    rows: int = 0

    @property
    def native(self) -> bool:
        return self.backend == RUST_COMPANION

    def entry(self) -> dict[str, Any]:
        ran = self.rows > 0
        reason = self.reason or (None if ran else _REASON_NO_ROWS)
        entry: dict[str, Any] = {
            "table": self.table,
            "column": self.column,
            "path": PATH_SCALAR,
            "executed_backend": self.backend if ran else _NO_KERNEL_RAN,
        }
        if reason is not None:
            entry["reason"] = reason
        return entry


@dataclass
class RemapRecord:
    """Orphan rows minted by re-running one parent column's hash, by backend."""

    table: str
    column: str
    native_rows: int = 0
    python_rows: int = 0
    reasons: list[str] = field(default_factory=list)

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


def _table_columns(plan: Plan, table: str) -> dict[str, ColumnSeed]:
    seed: TableSeed | None = next(
        (t for name, t in plan.seed_envelope.per_table if name == table), None
    )
    return dict(seed.per_column) if seed is not None else {}


class SeqNativeRun:
    """Run-scoped hash decisions for one `run_sequential` call.

    The companion loads at most once, on the first hash column that needs a decision. The
    decisions outlive the table that made them: REMAP on a later child table reads its
    parent's decision after the parent frame has been evicted."""

    def __init__(self, *, native_threads: int | None = None) -> None:
        self._native_threads = native_threads
        self._loaded = False
        self._bound: BoundHashKernel | None = None
        self._decisions: dict[tuple[str, str], ColumnDecision] = {}
        self._remap: dict[tuple[str, str], RemapRecord] = {}
        self._inherited: list[dict[str, Any]] = []
        self._in_remap = 0

    def _kernel(self) -> BoundHashKernel | None:
        if not self._loaded:
            self._loaded = True
            try:
                self._bound = BoundHashKernel(load_compiled_crypto_kernel(), self._native_threads)
            except CryptoExtensionUnavailableError:
                self._bound = None
        return self._bound

    def prepare_table(
        self, plan: Plan, table: str, source_schema: pa.Schema, graph: RelationshipGraph
    ) -> None:
        """Decide every scalar hash column of `table` and record its inherited FK children."""
        columns = _table_columns(plan, table)
        fk_children = {c for e in graph.edges if e.child_table == table for c in e.child_columns}
        for column, seed in columns.items():
            if seed.strategy != "hash" or column in fk_children:
                continue
            if source_schema.get_field_index(column) < 0:
                continue
            input_type = source_schema.field(column).type
            bound = self._kernel()
            reason = decline_reason(column, input_type) if bound is not None else REASON_UNAVAILABLE
            native = bound is not None and reason is None
            self._decisions[(table, column)] = ColumnDecision(
                table, column, RUST_COMPANION if native else PYTHON_SCALAR, reason, input_type
            )
        for edge in graph.edges:
            if edge.child_table != table:
                continue
            parent_columns = _table_columns(plan, edge.parent_table)
            for pcol, ccol in zip(edge.parent_columns, edge.child_columns, strict=True):
                parent_seed = parent_columns.get(pcol)
                if parent_seed is None or parent_seed.strategy != "hash":
                    continue
                parent = self._decisions.get((edge.parent_table, pcol))
                self._inherited.append(
                    {
                        "table": table,
                        "column": ccol,
                        "path": PATH_INHERITED,
                        "executed_backend": INHERITED,
                        "parent": f"{edge.parent_table}.{pcol}",
                        "parent_backend": parent.backend if parent is not None else None,
                    }
                )

    @contextmanager
    def remap_scope(self) -> Iterator[None]:
        """Mark the parent-handler calls made inside as REMAP minting, not parent derivation."""
        self._in_remap += 1
        try:
            yield
        finally:
            self._in_remap -= 1

    def hash_column(
        self,
        ctx: StrategyContext,
        df: pd.DataFrame,
        column: str,
        *,
        namespace: str,
        mask_key: bytes,
        truncate: int | None,
    ) -> list[Any] | None:
        """The tokens for `df[column]`, positionally, or None when the Python handler runs."""
        if ctx.nested_outer_column:
            return None  # a nested child hashes synthetic leaves, not a planned column
        if self._in_remap:
            return self._remap_tokens(
                ctx.current_table, column, df[column], namespace, mask_key, truncate
            )
        decision = self._decisions.get((ctx.current_table, column))
        if decision is None:
            return None
        rows = len(df)
        decision.rows += rows
        if not decision.native or rows == 0 or self._bound is None:
            return None
        values = self._pinned_array(df[column], decision)
        out = native_hash_array(
            values, bound=self._bound, mask_key=mask_key, namespace=namespace, truncate=truncate
        )
        return out.to_pylist()

    @staticmethod
    def _pinned_array(series: pd.Series[Any], decision: ColumnDecision) -> pa.Array:
        """Rebuild `series` as the column's pinned Arrow type, or abort the run."""
        failed = False
        try:
            values = pa.array(series, type=decision.pinned_type, from_pandas=True)
        except _CONVERSION_ERRORS:
            failed = True
        # Raised outside the handler: pyarrow's own message quotes the offending cell.
        if failed:
            raise ExecutionError(
                code="sequential_hash_input_type_drift",
                message=(
                    f"hash column {decision.column!r} of table {decision.table!r} was pinned as "
                    f"{decision.pinned_type} but its frame column no longer converts to it; the "
                    "run is aborted rather than changing path mid-column."
                ),
            )
        return values

    def _remap_tokens(
        self,
        table: str,
        column: str,
        series: pd.Series[Any],
        namespace: str,
        mask_key: bytes,
        truncate: int | None,
    ) -> list[Any] | None:
        record = self._remap.setdefault((table, column), RemapRecord(table, column))
        rows = len(series)
        parent = self._decisions.get((table, column))
        if parent is None or not parent.native or self._bound is None:
            record.python_rows += rows
            record.reasons.append(f"{REASON_REMAP_PARENT_PYTHON}:{column}")
            return None
        values = pandas_column_to_kernel_input(series)
        # The orphan keys are typed by what they are, not by the parent's width: an int8 parent
        # can legitimately be asked to mint a key above 127.
        reason = (
            decline_reason(column, values.type)
            if isinstance(values, pa.Array)
            else f"{REASON_REMAP_INPUT_NOT_ARROW}:{column}"
        )
        if reason is not None or not isinstance(values, pa.Array):
            record.python_rows += rows
            record.reasons.append(reason or "")
            return None
        out = native_hash_array(
            values, bound=self._bound, mask_key=mask_key, namespace=namespace, truncate=truncate
        )
        record.native_rows += rows
        return out.to_pylist()

    def metrics(self) -> dict[str, Any]:
        """The `quality_metrics` fragment: empty when the run hashed nothing."""
        entries = [d.entry() for d in self._decisions.values()]
        entries.extend(r.entry() for r in self._remap.values())
        entries.extend(self._inherited)
        return {SEQ_HASH_DISPOSITION_KEY: entries} if entries else {}


__all__ = [
    "INHERITED",
    "MIXED",
    "PATH_INHERITED",
    "PATH_REMAP",
    "PATH_SCALAR",
    "SEQ_HASH_DISPOSITION_KEY",
    "ColumnDecision",
    "RemapRecord",
    "SeqNativeRun",
]
