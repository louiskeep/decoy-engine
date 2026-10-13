"""Native routing for payload masks on the sequential (SEQ) FK route: `hash`, and `fpe`.

`run_sequential` masks one table at a time in pandas. For a hash column whose Arrow type the
compiled companion canonicalizes identically, the hash handler asks this module for the
tokens and gets them from the SAME keyed-derivation kernel the out-of-core route calls
(`out_of_core/_hash_native.py`: `native_hash_array`, `decline_reason`, reused unchanged), so no
keyed derivation is re-implemented here. The handler keeps its pandas orchestration; only the
token computation moves.

The per-strategy parts (kernel load, admission, kernel call, error channel, warnings) are one
`NativeOp` descriptor each (`_sequential_native_ops.py`), looked up by the column's strategy; a
strategy with no descriptor runs in Python. Kernel state is kept per strategy. Only hash may be an
FK key on this route: any other strategy's column that is an FK parent or child key gets no native
decision, because the orphan REMAP path re-runs the parent handler and is hash-only. The REMAP and
inherited-child machinery below is therefore hash-specific by design.

Three FK paths stay distinct, because only one of them calls the kernel at the scalar seam:

1. Parent or payload derivation: a hash column is masked at the handler. Native when its
   input type was admitted at table prep.
2. Matched child: `resolve_fk_keys` hands the child the parent's already-masked value. No
   kernel call and no re-derivation; the disposition reports the child as inherited, with the
   parent it came from.
3. REMAP: an orphan key is minted by running the PARENT's handler again (`make_remap_fn`).
   That call is admitted on its own, from the type of the orphan keys it actually holds, and
   only when the parent column's REMAP decision is native, so one column never mixes two
   Unicode normalizations.

A column has up to two decisions, because it can play two roles. The execution decision covers
hashing the column's own rows and exists only for a column that is not an FK child (a child
inherits). The REMAP decision covers re-deriving orphan keys for a child edge and exists for
every column that is the parent key of some edge, including a chain's middle column that is
also an FK child. Both come from the same pinned type and the same admission predicate, so a
middle column mints its orphans on the kernel without ever being rehashed as a matched child.

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
from decoy_engine.execution._sequential_native_ops import (
    BUCKET_PERTURB,
    CATEGORICAL,
    FPE,
    HASH,
    SEQ_FPE_DISPOSITION_KEY,
    SEQ_HASH_DISPOSITION_KEY,
    BoundFpeKernel,
    BucketCall,
    CategoricalCall,
    HashCall,
    NativeOp,
    bucket_perturb_op,
    categorical_op,
    fpe_op,
    hash_op,
)
from decoy_engine.execution.native._bucket_perturb_ext import native_range_exceeded
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.native._fpe_ext import load_compiled_fpe_kernel
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.execution.out_of_core._categorical_native import BoundIndexKernel
from decoy_engine.execution.out_of_core._hash_native import (
    REASON_UNAVAILABLE,
    BoundHashKernel,
    decline_reason,
    native_hash_array,
)

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import StrategyContext
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.plan._types import ColumnSeed, Plan, TableSeed
    from decoy_engine.relationships._graph import RelationshipGraph
    from decoy_engine.transforms.date_preserve import PreserveSet

PATH_SCALAR = "scalar"
PATH_REMAP = "fk_remap"
PATH_INHERITED = "fk_inherited"
INHERITED = "inherited"
MIXED = "mixed"
REASON_REMAP_PARENT_PYTHON = "hash_remap_parent_stays_python"
REASON_REMAP_INPUT_NOT_ARROW = "hash_remap_input_not_arrow"
_NO_KERNEL_RAN = "none"
_REASON_NO_ROWS = "no_rows_hashed"
_REASON_NO_BUCKET_CALLS = "no_native_bucket_calls"
REASON_BUCKET_OUT_OF_RANGE = "sequential_bucket_perturb_date_out_of_native_range"
_CONVERSION_ERRORS = (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError)


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
    # bucket_perturb only: evidence comes from the compiled calls the kernel reported, so a
    # column that parsed nothing never claims a kernel ran; `counts_preserved` is set once the
    # native path has recorded its sentinel count.
    native_calls: int = 0
    counts_preserved: bool = False
    preserved_rows: int = 0

    @property
    def native(self) -> bool:
        return self.backend == RUST_COMPANION

    def entry(self) -> dict[str, Any]:
        calls_decide = self.strategy == BUCKET_PERTURB and self.native
        ran = self.native_calls > 0 if calls_decide else self.rows > 0
        none_reason = _REASON_NO_BUCKET_CALLS if calls_decide and self.rows else _REASON_NO_ROWS
        reason = self.reason or (None if ran else none_reason)
        entry: dict[str, Any] = {
            "table": self.table,
            "column": self.column,
            "path": PATH_SCALAR,
            "executed_backend": self.backend if ran else _NO_KERNEL_RAN,
        }
        if reason is not None:
            entry["reason"] = reason
        if self.counts_preserved:
            entry["preserved_rows"] = self.preserved_rows
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


# Loaders live here, not in the ops module, so each is resolved through this module's globals
# at call time (a test swaps the companion load by patching the name).
def _load_hash(native_threads: int | None) -> BoundHashKernel:
    return BoundHashKernel(load_compiled_crypto_kernel(), native_threads)


def _load_fpe(native_threads: int | None) -> BoundFpeKernel:
    return BoundFpeKernel(load_compiled_fpe_kernel(), native_threads)


def _load_categorical(native_threads: int | None) -> BoundIndexKernel:
    return BoundIndexKernel(load_compiled_index_kernel(), native_threads)


def _load_bucket(native_threads: int | None) -> BoundIndexKernel:
    return BoundIndexKernel(load_compiled_index_kernel(), native_threads)


_OPS: dict[str, NativeOp] = {
    HASH: hash_op(_load_hash),
    FPE: fpe_op(_load_fpe),
    CATEGORICAL: categorical_op(_load_categorical),
    BUCKET_PERTURB: bucket_perturb_op(_load_bucket),
}


class SeqNativeRun:
    """Run-scoped native decisions for one `run_sequential` call.

    Each strategy's companion kernel loads at most once, on the first column of that strategy
    that needs a decision. The decisions outlive the table that made them: REMAP on a later
    child table reads its parent's decision after the parent frame has been evicted."""

    def __init__(self, *, native_threads: int | None = None) -> None:
        self._native_threads = native_threads
        self._load_attempted: set[str] = set()
        self._bound: dict[str, Any] = {}
        self._decisions: dict[tuple[str, str], ColumnDecision] = {}
        self._remap_decisions: dict[tuple[str, str], ColumnDecision] = {}
        self._inherited_columns: set[tuple[str, str]] = set()
        self._remap: dict[tuple[str, str], RemapRecord] = {}
        self._inherited: list[dict[str, Any]] = []
        self._in_remap = 0

    def _kernel(self, op: NativeOp) -> Any | None:
        if op.strategy not in self._load_attempted:
            self._load_attempted.add(op.strategy)
            try:
                self._bound[op.strategy] = op.load_kernel(self._native_threads)
            except CryptoExtensionUnavailableError:
                pass
        return self._bound.get(op.strategy)

    def _decide(
        self, op: NativeOp, table: str, column: str, input_type: pa.DataType, seed: ColumnSeed
    ) -> ColumnDecision:
        bound = self._kernel(op)
        reason = op.admit(column, input_type, seed) if bound is not None else REASON_UNAVAILABLE
        native = bound is not None and reason is None
        return ColumnDecision(
            table,
            column,
            RUST_COMPANION if native else PYTHON_SCALAR,
            reason,
            input_type,
            strategy=op.strategy,
        )

    def prepare_table(
        self, plan: Plan, table: str, source_schema: pa.Schema, graph: RelationshipGraph
    ) -> None:
        """Decide every native-capable column of `table` and record its inherited FK children.

        A hash column gets an execution decision unless it is an FK child, and a REMAP decision
        when some edge uses it as a parent key. A column that is both gets only the REMAP one.
        Any other native-capable strategy gets an execution decision only when it is no FK key
        at all, so the hash-only REMAP path never reaches it."""
        columns = _table_columns(plan, table)
        fk_children = {c for e in graph.edges if e.child_table == table for c in e.child_columns}
        parent_keys = {c for e in graph.edges if e.parent_table == table for c in e.parent_columns}
        for column, seed in columns.items():
            op = _OPS.get(seed.strategy)
            if op is None or source_schema.get_field_index(column) < 0:
                continue
            if op.strategy != HASH:
                if column not in fk_children and column not in parent_keys:
                    self._decisions[(table, column)] = self._decide(
                        op, table, column, source_schema.field(column).type, seed
                    )
                continue
            is_child = column in fk_children
            if is_child:
                self._inherited_columns.add((table, column))
            if is_child and column not in parent_keys:
                continue  # inherits, and no edge re-derives it: nothing to decide
            decision = self._decide(op, table, column, source_schema.field(column).type, seed)
            if not is_child:
                self._decisions[(table, column)] = decision
            if column in parent_keys:
                self._remap_decisions[(table, column)] = decision
        for edge in graph.edges:
            if edge.child_table != table:
                continue
            parent_columns = _table_columns(plan, edge.parent_table)
            for pcol, ccol in zip(edge.parent_columns, edge.child_columns, strict=True):
                parent_seed = parent_columns.get(pcol)
                if parent_seed is None or parent_seed.strategy != "hash":
                    continue
                parent = self._decisions.get((edge.parent_table, pcol))
                parent_backend = (
                    parent.backend
                    if parent is not None
                    else INHERITED
                    if (edge.parent_table, pcol) in self._inherited_columns
                    else None
                )
                self._inherited.append(
                    {
                        "table": table,
                        "column": ccol,
                        "path": PATH_INHERITED,
                        "executed_backend": INHERITED,
                        "parent": f"{edge.parent_table}.{pcol}",
                        "parent_backend": parent_backend,
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
        return self.native_column(ctx, df, column, HASH, HashCall(namespace, mask_key, truncate))

    def categorical_column(
        self,
        ctx: StrategyContext,
        df: pd.DataFrame,
        column: str,
        *,
        namespace: str,
        mask_key: bytes,
        categories: list[Any],
        cdf: list[int] | None,
    ) -> list[Any] | None:
        """The tokens for `df[column]`, positionally, or None when the Python handler runs.

        The handler's own resolved pool (categories, and the CDF when weighted) is the call
        payload; admission already proved the pool is all-string and the CDF buildable."""
        call = CategoricalCall(
            namespace,
            mask_key,
            tuple(categories),
            None if cdf is None else tuple(cdf),
        )
        return self.native_column(ctx, df, column, CATEGORICAL, call)

    def bucket_column(
        self,
        ctx: StrategyContext,
        df: pd.DataFrame,
        column: str,
        *,
        namespace: str,
        mask_key: bytes,
        bucket: str,
        date_format: str | None,
        preserve: PreserveSet | None,
    ) -> list[Any] | None:
        """The perturbed dates for `df[column]`, positionally, or None when Python runs.

        Order matters: the sentinel mask comes from the original column first, the range check
        sees only the rows the kernel will see, and the preserved count is recorded once, only
        by the native path, so a declined column's count is left to the Python handler."""
        if ctx.nested_outer_column or self._in_remap:
            return None
        decision = self._decisions.get((ctx.current_table, column))
        if decision is None or decision.strategy != BUCKET_PERTURB:
            return None
        rows = len(df)
        decision.rows += rows
        bound = self._bound.get(BUCKET_PERTURB)
        if not decision.native or rows == 0 or bound is None:
            return None
        if date_format is None:  # pragma: no cover - admission requires an explicit format
            raise AssertionError("bucket_perturb admitted without a date_format.")
        preserved = preserve.mask(df[column]) if preserve is not None else None
        array = self._pinned_array(df[column], decision)
        rest = array if preserved is None else array.filter(pa.array(~preserved))
        if native_range_exceeded(rest, bucket, date_format):
            # Python's date arithmetic covers the whole calendar; native does not. Declining
            # keeps this route a superset of what the Python path accepts.
            decision.backend, decision.reason = PYTHON_SCALAR, REASON_BUCKET_OUT_OF_RANGE
            return None
        call = BucketCall(namespace, mask_key, bucket, date_format, column, preserved, [])
        result = _OPS[BUCKET_PERTURB].apply(bound, array, call)
        decision.native_calls += sum(call.derive_calls)
        if preserved is not None:
            count = int(preserved.sum())
            decision.counts_preserved = True
            decision.preserved_rows += count
            ctx.record_date_preserved(column, count)
        return result.values.to_pylist()

    def native_column(
        self, ctx: StrategyContext, df: pd.DataFrame, column: str, strategy: str, call: Any
    ) -> list[Any] | None:
        """The native output for `df[column]`, positionally, or None when the Python handler runs.

        A kernel error channel is checked before any value leaves: a strategy that declares
        errors raises its coded exception, so the failed table publishes nothing."""
        if ctx.nested_outer_column or self._in_remap:
            return None  # a non-hash REMAP call returns to Python before touching hash state
        decision = self._decisions.get((ctx.current_table, column))
        if decision is None or decision.strategy != strategy:
            return None
        rows = len(df)
        decision.rows += rows
        bound = self._bound.get(strategy)
        if not decision.native or rows == 0 or bound is None:
            return None
        op = _OPS[strategy]
        result = op.apply(bound, self._pinned_array(df[column], decision), call)
        if result.errors:
            if op.fail_closed is None:  # pragma: no cover - only a descriptor with errors sets them
                raise AssertionError(f"{strategy} kernel returned errors with no fail-closed rule")
            raise op.fail_closed(result.errors, column)
        return result.values.to_pylist()

    def warnings(
        self, strategy: str, original: list[Any], call: Any, column: str
    ) -> list[QualityWarning]:
        """The handler-side warnings of a natively masked column, from its ORIGINAL values."""
        warn = _OPS[strategy].warn
        return list(warn(original, call, column)) if warn is not None else []

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
                code=f"sequential_{decision.strategy}_input_type_drift",
                message=(
                    f"{decision.strategy} column {decision.column!r} of table {decision.table!r} was pinned as "
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
        parent = self._remap_decisions.get((table, column))
        bound = self._bound.get(HASH)
        if parent is None or not parent.native or bound is None:
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
            values, bound=bound, mask_key=mask_key, namespace=namespace, truncate=truncate
        )
        record.native_rows += rows
        return out.to_pylist()

    def metrics(self) -> dict[str, Any]:
        """The `quality_metrics` fragment: one key per registered strategy (`sequential_hash`,
        `sequential_fpe`, `sequential_categorical`). A key holds one disposition entry per
        recorded decision for that strategy - scalar for every strategy, plus REMAP and
        inherited for hash - whatever the backend; read `executed_backend` on an entry to see
        whether it ran native, in Python, or was inherited. A key is absent only when the
        strategy recorded no decision at all (e.g. no column used it)."""
        metrics: dict[str, Any] = {}
        for op in _OPS.values():
            entries = [d.entry() for d in self._decisions.values() if d.strategy == op.strategy]
            if op.strategy == HASH:
                entries.extend(r.entry() for r in self._remap.values())
                entries.extend(self._inherited)
            if entries:
                metrics[op.disposition_key] = entries
        return metrics


__all__ = [
    "INHERITED",
    "MIXED",
    "PATH_INHERITED",
    "PATH_REMAP",
    "PATH_SCALAR",
    "SEQ_FPE_DISPOSITION_KEY",
    "SEQ_HASH_DISPOSITION_KEY",
    "ColumnDecision",
    "RemapRecord",
    "SeqNativeRun",
]
