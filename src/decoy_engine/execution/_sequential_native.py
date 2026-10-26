"""Native routing for payload masks on the sequential (SEQ) FK route.

`run_sequential` masks one table at a time in pandas. This module is the dispatcher that lets a
handler ask for a native result instead of computing it in Python, reusing the SAME keyed
kernels the out-of-core route calls (no keyed derivation is re-implemented here); the handler
keeps its pandas orchestration. The per-strategy parts (kernel load, admission, kernel call,
error channel, warnings) are one `NativeOp` descriptor each in `_sequential_native_ops.py`,
looked up by the column's strategy; a strategy with no descriptor runs in Python. Kernel state
is kept per strategy. Two strategies keep their seam in a sibling module because their shape
does not fit the per-value descriptor: `text_mask` (`_sequential_native_text_mask.py`) and
`group_key` (`_sequential_native_group_key.py`, whose admission reads a sibling column).

Only hash and fpe may be native FK keys on this route (`_KEY_STRATEGIES`): both are injective,
deterministic per-value maps whose orphan REMAP minting re-runs the PARENT's handler through the
same kernel. Any other native-capable strategy's column that is an FK parent or child key stays
Python with a reporting-only decline (`REASON_KEY_NOT_INJECTIVE`), because its output is not a
guaranteed unique key. The REMAP and inherited-child machinery serves the key strategies, each
entry attributed to the parent's strategy. Three FK paths stay distinct, because only one calls
the kernel at the scalar seam: (1) parent/payload derivation, masked at the handler and native
when the input type was admitted at table prep; (2) a matched child, handed the parent's
already-masked value by `resolve_fk_keys` (no kernel call, reported as inherited); (3) REMAP,
which mints an orphan key by re-running the parent's handler (`make_remap_fn`), admitted on its
own from the orphan keys' type and only when the parent's REMAP decision is native, so one
column never mixes two Unicode normalizations. A column thus has up to two decisions (execution
and REMAP); both come from the same pinned type and admission predicate.

Boundary with pandas: the kernel is Arrow in, Arrow out, but the handler holds a pandas column.
Input is rebuilt as an Arrow array of the column's PINNED source type (read from the Arrow
source at table prep), never inferred from pandas (inference would turn `large_string` into
`string` and an all-null column into `null`); output is a plain list assigned positionally, so a
`when` selection with a gapped index lands on the right rows. There is no fallback: a kernel
fault, or a column that stops converting, aborts the run, so no column is ever part native and
part Python. Reason codes carry the strategy outcome, the column and the Arrow type only, never
a cell, key or derived value.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pandas as pd
import pyarrow as pa

from decoy_engine.execution._adapter import pandas_column_to_kernel_input
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution._sequential_native_decisions import (
    MIXED,
    PATH_REMAP,
    PATH_SCALAR,
    ColumnDecision,
    RemapRecord,
)
from decoy_engine.execution._sequential_native_group_key import (
    GROUP_KEY,
    decide_group_key,
    group_key_op,
    load_raw_hex,
)
from decoy_engine.execution._sequential_native_ops import (
    BUCKET_PERTURB,
    CATEGORICAL,
    DATE_SHIFT,
    FAKER,
    FAKER_DETERMINISTIC,
    FAKER_POSITIONAL,
    FPE,
    HASH,
    SEQ_FPE_DISPOSITION_KEY,
    SEQ_HASH_DISPOSITION_KEY,
    TEXT_REDACT,
    BoundFpeKernel,
    BucketCall,
    CategoricalCall,
    FakerCall,
    HashCall,
    NativeOp,
    TextRedactCall,
    _load_text_redact,
    bucket_perturb_op,
    categorical_op,
    date_shift_op,
    date_shift_row_errors,
    faker_op,
    fpe_op,
    hash_op,
    integer_key_representation_lost,
    text_redact_op,
)
from decoy_engine.execution._sequential_native_text_mask import (
    TEXT_MASK,
    TextMaskCall,
    text_mask_op,
)
from decoy_engine.execution._strategies._faker_positional import resolve_selection_namespace
from decoy_engine.execution.native._bucket_perturb_ext import native_range_exceeded
from decoy_engine.execution.native._chunk_masking import pool_values_are_strings
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.native._date_shift_ext import native_date_shift
from decoy_engine.execution.native._fpe_ext import load_compiled_fpe_kernel
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.execution.native._text_mask_route import text_mask_sub_floor_warning
from decoy_engine.execution.out_of_core._categorical_native import BoundIndexKernel
from decoy_engine.execution.out_of_core._hash_native import (
    REASON_UNAVAILABLE,
    BoundHashKernel,
)

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import StrategyContext
    from decoy_engine.generation.pool import ValuePool
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.plan._types import ColumnSeed, Plan, TableSeed
    from decoy_engine.relationships._graph import RelationshipGraph
    from decoy_engine.transforms.date_preserve import PreserveSet

PATH_INHERITED = "fk_inherited"
PATH_KEY_DECLINED = "fk_key_declined"
INHERITED = "inherited"
REASON_REMAP_PARENT_PYTHON = "remap_parent_stays_python"
REASON_REMAP_INPUT_NOT_ARROW = "remap_input_not_arrow"
REASON_KEY_NOT_INJECTIVE = "sequential_fk_key_injectivity_not_guaranteed"
# Injective per-value maps: a masked FK key stays unique and joinable. Everything else native
# is deterministic but may collide, so it is not accelerated as a key.
_KEY_STRATEGIES = (HASH, FPE)
REASON_BUCKET_OUT_OF_RANGE = "sequential_bucket_perturb_date_out_of_native_range"
_CONVERSION_ERRORS = (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError)


def _declined_entry(table: str, column: str, reason: str) -> dict[str, Any]:
    return {
        "table": table,
        "column": column,
        "path": PATH_KEY_DECLINED,
        "executed_backend": PYTHON_SCALAR,
        "reason": reason,
    }


def _table_columns(plan: Plan, table: str) -> dict[str, ColumnSeed]:
    seed: TableSeed | None = next(
        (t for name, t in plan.seed_envelope.per_table if name == table), None
    )
    return dict(seed.per_column) if seed is not None else {}


# Loaders live here, not in the ops module, so each resolves through this module's globals at
# call time (a test swaps the companion load by patching the name).
def _load_hash(native_threads: int | None) -> BoundHashKernel:
    return BoundHashKernel(load_compiled_crypto_kernel(), native_threads)


def _load_fpe(native_threads: int | None) -> BoundFpeKernel:
    return BoundFpeKernel(load_compiled_fpe_kernel(), native_threads)


def _load_index(native_threads: int | None) -> BoundIndexKernel:
    return BoundIndexKernel(load_compiled_index_kernel(), native_threads)


_OPS: dict[str, NativeOp] = {
    HASH: hash_op(_load_hash),
    FPE: fpe_op(_load_fpe),
    CATEGORICAL: categorical_op(_load_index),
    BUCKET_PERTURB: bucket_perturb_op(_load_index),
    DATE_SHIFT: date_shift_op(_load_index),
    FAKER: faker_op(_load_index),
    TEXT_REDACT: text_redact_op(_load_text_redact),
    TEXT_MASK: text_mask_op(_load_text_redact),
    GROUP_KEY: group_key_op(load_raw_hex),
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
        self._inherited: list[tuple[str, dict[str, Any]]] = []
        self._key_declines: list[tuple[str, dict[str, Any]]] = []
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
        self,
        op: NativeOp,
        table: str,
        column: str,
        input_type: pa.DataType,
        seed: ColumnSeed,
        *,
        is_key: bool = False,
    ) -> ColumnDecision:
        bound = self._kernel(op)
        admit = op.key_admit if is_key and op.key_admit is not None else op.admit
        reason = admit(column, input_type, seed) if bound is not None else REASON_UNAVAILABLE
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

        A key-strategy column (hash, fpe) gets an execution decision unless it is an FK child,
        and a REMAP decision when some edge uses it as a parent key. A column that is both gets
        only the REMAP one. Any other native-capable strategy gets an execution decision only
        when it is no FK key at all; as a key it stays Python with a reporting-only decline."""
        columns = _table_columns(plan, table)
        fk_children = {c for e in graph.edges if e.child_table == table for c in e.child_columns}
        parent_keys = {c for e in graph.edges if e.parent_table == table for c in e.parent_columns}
        for column, seed in columns.items():
            op = _OPS.get(seed.strategy)
            if op is None or source_schema.get_field_index(column) < 0:
                continue
            is_child = column in fk_children
            input_type = source_schema.field(column).type
            if op.strategy not in _KEY_STRATEGIES:
                if column in fk_children or column in parent_keys:
                    reason = f"{REASON_KEY_NOT_INJECTIVE}:{op.strategy}:{column}:{input_type}"
                    self._key_declines.append((op.strategy, _declined_entry(table, column, reason)))
                elif op.strategy == GROUP_KEY:
                    # group_key's admission reads the SIBLING column's type and role, which the
                    # per-target `admit` cannot see; decide it from the source schema. The
                    # companion load stays shared across ops through `self._kernel`.
                    self._decisions[(table, column)] = decide_group_key(
                        self._kernel(op),
                        op.strategy,
                        table,
                        column,
                        input_type,
                        seed,
                        source_schema,
                        columns,
                        fk_children,
                        parent_keys,
                    )
                else:
                    self._decisions[(table, column)] = self._decide(
                        op, table, column, input_type, seed
                    )
                continue
            if is_child:
                self._inherited_columns.add((table, column))
            if is_child and column not in parent_keys:
                continue  # inherits, and no edge re-derives it: nothing to decide
            # A child that reaches here is also a parent key (a bare child `continue`d above), so
            # parent-key membership alone separates a key from a payload that shares the strategy.
            is_key = column in parent_keys
            decision = self._decide(op, table, column, input_type, seed, is_key=is_key)
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
                if parent_seed is None or parent_seed.strategy not in _KEY_STRATEGIES:
                    continue
                parent = self._decisions.get((edge.parent_table, pcol))
                parent_backend = (
                    parent.backend
                    if parent is not None
                    else INHERITED
                    if (edge.parent_table, pcol) in self._inherited_columns
                    else None
                )
                entry = {
                    "table": table,
                    "column": ccol,
                    "path": PATH_INHERITED,
                    "executed_backend": INHERITED,
                    "parent": f"{edge.parent_table}.{pcol}",
                    "parent_backend": parent_backend,
                }
                self._inherited.append((parent_seed.strategy, entry))

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
        exact: pa.ChunkedArray | None = None,
    ) -> list[Any] | None:
        """The tokens for `df[column]`, positionally, or None when the Python handler runs.

        `exact` is the column's recorded typed Arrow values, used in place of the frame column
        when the frame widened an integer column with nulls to float."""
        call = HashCall(namespace, mask_key, truncate)
        return self.key_column(ctx, df, column, HASH, call, exact)

    def key_column(
        self,
        ctx: StrategyContext,
        df: pd.DataFrame,
        column: str,
        strategy: str,
        call: Any,
        exact: pa.ChunkedArray | None = None,
    ) -> list[Any] | None:
        """A key-strategy column's output: orphan REMAP minting inside `remap_scope`, else the
        ordinary native derivation. None when the Python handler runs."""
        if ctx.nested_outer_column:
            return None  # a nested child masks synthetic leaves, not a planned column
        if self._in_remap:
            # `ctx.current_table` and `column` are the PARENT's here (make_remap_fn), and `call`
            # was built from the parent column's seed, so the tweak is the parent's.
            return self._remap_tokens(ctx.current_table, column, df[column], strategy, call)
        return self.native_column(ctx, df, column, strategy, call, exact)

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
        gate = self.admitted(ctx, column, BUCKET_PERTURB, len(df))
        if gate is None:
            return None
        decision, bound = gate
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
        call = BucketCall(namespace, mask_key, bucket, date_format, preserved, [])
        result = _OPS[BUCKET_PERTURB].apply(bound, array, call)
        decision.native_calls += sum(call.derive_calls)
        if preserved is not None:
            count = int(preserved.sum())
            decision.counts_preserved = True
            decision.preserved_rows += count
            ctx.record_date_preserved(column, count)
        return result.values.to_pylist()

    def faker_column(
        self, ctx: StrategyContext, df: pd.DataFrame, column: str, plan: ColumnSeed, pool: ValuePool
    ) -> list[Any] | None:
        """The pool picks for `df[column]`, positionally, or None when the Python handler runs.

        The handler built `pool` (and left it in `ctx.pool_cache`), so a decline here is a cache
        hit for the Python assembly. Admission saw only the seed; whether the BUILT pool holds
        strings is known now, and a caller-overridden registry binding of an allowlisted name
        may not, so that is the last decline before the kernel."""
        gate = self.admitted(ctx, column, FAKER, len(df))
        if gate is None:
            return None
        decision, bound = gate
        if not pool_values_are_strings(pool):
            decision.backend = PYTHON_SCALAR
            decision.reason = f"faker_provider_output_not_string:{column}:{plan.provider}"
            return None
        if plan.deterministic:
            call = FakerCall(FAKER_DETERMINISTIC, pool, str(plan.namespace), ctx.mask_key)
        else:
            call = FakerCall(
                FAKER_POSITIONAL,
                pool,
                resolve_selection_namespace(ctx, column, plan.namespace),
                ctx.job_seed,
                ctx.row_offset,
                getattr(ctx, "gate_positions", None),
            )
        result = _OPS[FAKER].apply(bound, self._pinned_array(df[column], decision), call)
        decision.native_calls += 1  # the helpers return no count; each makes one batch call
        return result.values.to_pylist()

    def text_redact_column(
        self,
        ctx: StrategyContext,
        df: pd.DataFrame,
        column: str,
        *,
        detectors: tuple[str, ...] | None,
        token: str,
        label_token: bool,
    ) -> list[Any] | None:
        """The redacted text for `df[column]`, positionally, or None when Python runs.

        The handler's resolved config is the payload (None detectors = all, never `()`);
        `native_text_redact` replays lookaround and non-ASCII cells itself, byte for byte."""
        gate = self.admitted(ctx, column, TEXT_REDACT, len(df))
        if gate is None:
            return None
        decision, bound = gate
        call = TextRedactCall(detectors, token, label_token)
        result = _OPS[TEXT_REDACT].apply(bound, self._pinned_array(df[column], decision), call)
        decision.native_calls += 1  # one batch call; an all-null column still runs it
        return result.values.to_pylist()

    def text_mask_column(
        self, ctx: StrategyContext, df: pd.DataFrame, column: str, **config: Any
    ) -> tuple[list[Any], list[QualityWarning]] | None:
        """The masked text and its sub-floor warning for `df[column]`, or None when Python runs.

        `config` is the handler's resolved `TextMaskCall` fields (None detectors = all, never
        `()`). The warning is built from the counts the one native call returned, not by
        re-masking."""
        gate = self.admitted(ctx, column, TEXT_MASK, len(df))
        if gate is None:
            return None
        decision, bound = gate
        call = TextMaskCall(column, **config)
        result = _OPS[TEXT_MASK].apply(bound, self._pinned_array(df[column], decision), call)
        decision.native_calls += 1  # one batch call; an all-null column still runs it
        warnings = []
        if result.notices:
            warnings.append(
                text_mask_sub_floor_warning(
                    result.notices, policy=call.sub_floor_span_policy, column=column
                )
            )
        return result.values.to_pylist(), warnings

    def admitted(
        self, ctx: StrategyContext, column: str, strategy: str, rows: int
    ) -> tuple[ColumnDecision, Any] | None:
        """The column's decision and bound kernel when this call may run native, else None.

        A nested child or a REMAP call returns to Python before touching native state; the
        row count is booked whether or not the column ends up native."""
        if ctx.nested_outer_column or self._in_remap:
            return None
        decision = self._decisions.get((ctx.current_table, column))
        if decision is None or decision.strategy != strategy:
            return None
        decision.rows += rows
        bound = self._bound.get(strategy)
        if not decision.native or rows == 0 or bound is None:
            return None
        return decision, bound

    def date_shift_column(
        self,
        ctx: StrategyContext,
        df: pd.DataFrame,
        column: str,
        *,
        namespace: str,
        mask_key: bytes,
        min_days: int,
        max_days: int,
        date_format: str | None,
        preserve: PreserveSet | None,
    ) -> list[Any] | None:
        """The shifted dates for `df[column]`, positionally, or None when Python runs.

        Unparseable cells keep their text and are appended to `ctx.row_errors` as the handler
        does; the pipeline decides fail versus quarantine. Positions need no rebasing: the
        kernel rebases past sentinel rows and the `when` gate maps back to table positions."""
        gate = self.admitted(ctx, column, DATE_SHIFT, len(df))
        if gate is None:
            return None
        decision, bound = gate
        if date_format is None:  # pragma: no cover - admission requires an explicit format
            raise AssertionError("date_shift admitted without a date_format.")
        preserved = preserve.mask(df[column]) if preserve is not None else None
        array = self._pinned_array(df[column], decision)
        derive_calls: list[int] = []
        out, positions = native_date_shift(
            array,
            min_days=min_days,
            max_days=max_days,
            date_format=date_format,
            mask_key=mask_key,
            namespace=namespace,
            index_kernel=bound.kernel,
            native_threads=bound.native_threads,
            derive_calls=derive_calls,
            preserved=preserved,
        )
        decision.native_calls += sum(derive_calls)
        ctx.row_errors.extend(date_shift_row_errors(column, positions))
        if preserved is not None:
            count = int(preserved.sum())
            decision.counts_preserved = True
            decision.preserved_rows += count
            if count:  # the Python handler records a count only when a sentinel matched
                ctx.record_date_preserved(column, count)
        return out.to_pylist()

    def native_column(
        self,
        ctx: StrategyContext,
        df: pd.DataFrame,
        column: str,
        strategy: str,
        call: Any,
        exact: pa.ChunkedArray | None = None,
    ) -> list[Any] | None:
        """The native output for `df[column]`, positionally, or None when the Python handler runs.

        A kernel error channel is checked before any value leaves: a strategy that declares
        errors raises its coded exception, so the failed table publishes nothing."""
        gate = self.admitted(ctx, column, strategy, len(df))
        if gate is None:
            return None
        decision, bound = gate
        op = _OPS[strategy]
        if integer_key_representation_lost(strategy, decision.pinned_type, df[column].dtype):
            raise ExecutionError(
                code="sequential_fpe_key_integer_not_preserved",
                message=(
                    f"fpe key column {column!r} was admitted as {decision.pinned_type} but its "
                    "frame column is no longer integer; the run is aborted."
                ),
            )
        values = (
            exact.combine_chunks()
            if exact is not None and exact.type == decision.pinned_type
            else self._pinned_array(df[column], decision)
        )
        result = op.apply(bound, values, call)
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
        self, table: str, column: str, series: pd.Series[Any], strategy: str, call: Any
    ) -> list[Any] | None:
        record = self._remap.setdefault(
            (table, column), RemapRecord(table, column, strategy=strategy)
        )
        rows = len(series)
        parent = self._remap_decisions.get((table, column))
        bound = self._bound.get(strategy)
        if parent is None or not parent.native or bound is None:
            record.python_rows += rows
            record.reasons.append(f"{strategy}_{REASON_REMAP_PARENT_PYTHON}:{column}")
            return None
        values = pandas_column_to_kernel_input(series)
        # The orphan keys are typed by what they are, not by the parent's width: an int8 parent
        # can legitimately be asked to mint a key above 127.
        op = _OPS[strategy]
        if not isinstance(values, pa.Array):
            reason: str | None = f"{strategy}_{REASON_REMAP_INPUT_NOT_ARROW}:{column}"
        else:
            if op.remap_input_reason is None:  # pragma: no cover - only key strategies reach here
                raise AssertionError(f"{strategy} has no REMAP admission rule")
            reason = op.remap_input_reason(column, values.type)
        if reason is not None or not isinstance(values, pa.Array):
            record.python_rows += rows
            record.reasons.append(reason or "")
            return None
        # Checked before any value returns, exactly as the scalar path: a bad orphan key must
        # fail the table closed here too, not leak a half-minted key.
        result = op.apply(bound, values, call)
        if result.errors:
            if op.fail_closed is None:  # pragma: no cover - only a descriptor with errors sets them
                raise AssertionError(f"{strategy} kernel returned errors with no fail-closed rule")
            raise op.fail_closed(result.errors, column)
        record.native_rows += rows
        return result.values.to_pylist()

    def metrics(self) -> dict[str, Any]:
        """The `quality_metrics` fragment: one key per registered strategy (`sequential_hash`,
        `sequential_fpe`, `sequential_categorical`). A key holds one disposition entry per
        recorded decision for that strategy - scalar for every strategy, plus REMAP and
        inherited for the key strategies (hash, fpe), plus a reporting-only decline for each
        FK key column of a strategy that cannot be a native key. Read `executed_backend` on an
        entry to see whether it ran native, in Python, or was inherited. A key is absent only
        when the strategy recorded no decision at all (e.g. no column used it)."""
        metrics: dict[str, Any] = {}
        for op in _OPS.values():
            entries = [d.entry() for d in self._decisions.values() if d.strategy == op.strategy]
            entries.extend(r.entry() for r in self._remap.values() if r.strategy == op.strategy)
            entries.extend(e for strategy, e in self._inherited if strategy == op.strategy)
            entries.extend(e for strategy, e in self._key_declines if strategy == op.strategy)
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
