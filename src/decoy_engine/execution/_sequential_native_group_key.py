"""Native group_key payload on the sequential (SEQ) FK route (B7).

Kept in its own module (like `_sequential_native_text_mask.py`) because group_key is the one
SEQ payload strategy whose native admission reads a SIBLING column, not its own: the key is
derived from each row's `group_by` value, so neither the per-target
`admit(column, input_type, seed)` nor the per-value `apply` seam of `_sequential_native_ops.py`
fits. The op descriptor's admit/apply slots are therefore inert (they assert), and the
sibling-aware decision (`decide_group_key`) and the derivation (`group_key_column`) live here,
called by `SeqNativeRun.prepare_table` and the `GroupKeyStrategyHandler` seam.

The derivation re-implements no keyed primitive: it reuses the shared raw-hex kernel through
`_group_key_kernel` (`stringify_group_by_series` + `derive_group_key_from_strings`, the same
helpers `native_group_key` uses). The sibling is stringified exactly as the oracle's
`to_pandas_fk_safe` frame does (a null-bearing int64 sibling is the nullable `Int64` dtype, not
float64-widened), then the raw-hex derivation produces the key; output is a positional list,
matching the Python handler, so a `when` selection lands on the right rows.

Reason codes carry the strategy outcome, the column and the Arrow type only, never a cell, key
or derived value.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution._sequential_native_decisions import ColumnDecision
from decoy_engine.execution._sequential_native_ops import NativeOp, NativeResult
from decoy_engine.execution.native._group_key_ext import (
    RawHexDerivationKernel,
    load_compiled_raw_hex_kernel,
)
from decoy_engine.execution.native._group_key_kernel import (
    derive_group_key_from_strings,
    stringify_group_by_series,
)
from decoy_engine.execution.native._operator_config_rejections import (
    group_key_sibling_type_admitted,
)
from decoy_engine.execution.out_of_core._hash_native import REASON_UNAVAILABLE
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.group_key import GroupKeyConfig

if TYPE_CHECKING:
    import pandas as pd

    from decoy_engine.execution._adapter import StrategyContext
    from decoy_engine.execution._sequential_native import SeqNativeRun
    from decoy_engine.plan._types import ColumnSeed

GROUP_KEY = "group_key"
SEQ_GROUP_KEY_DISPOSITION_KEY = "sequential_group_key"


@dataclass(frozen=True)
class BoundRawHexKernel:
    """The compiled raw-hex (canonicalize-free) kernel group_key selects with, and the thread
    budget it runs with."""

    kernel: RawHexDerivationKernel
    native_threads: int | None = None


def load_raw_hex(native_threads: int | None) -> BoundRawHexKernel:
    return BoundRawHexKernel(load_compiled_raw_hex_kernel(), native_threads)


def _group_key_admit_unused(column: str, input_type: pa.DataType, seed: ColumnSeed) -> str | None:
    # group_key's admission reads the SIBLING column's type, not the target's, so it is decided in
    # SeqNativeRun.prepare_table with the source schema, never through this per-target admit.
    raise AssertionError("group_key uses prepare_table sibling admission, not admit")


def _group_key_apply_unused(bound: Any, values: pa.Array, call: Any) -> NativeResult:
    # group_key reads a sibling column, so the seam calls group_key_column directly; reaching this
    # slot means the routing was bypassed.
    raise AssertionError("group_key uses group_key_column, not apply")


def group_key_op(load_kernel: Callable[[int | None], Any]) -> NativeOp:
    return NativeOp(
        GROUP_KEY,
        SEQ_GROUP_KEY_DISPOSITION_KEY,
        load_kernel,
        _group_key_admit_unused,
        _group_key_apply_unused,
    )


def group_key_native_reason(
    column: str,
    seed: ColumnSeed,
    source_schema: pa.Schema,
    table_columns: dict[str, ColumnSeed],
    fk_children: set[str],
    parent_keys: set[str],
) -> str | None:
    """The coded reason a group_key column stays on the Python handler, or None when its
    `group_by` sibling admits it to the raw-hex kernel.

    Mirrors the chunked/full-frame gates, read from the plan + source schema: the sibling must
    be present, not the column itself (self-anchor), unmasked (a passthrough node, or a source
    column no node masks), not an FK-rewritten column, and of an admitted Arrow type
    ({string, int64, bool}). An invalid config declines so the Python handler raises the
    oracle's own coded error rather than this path swallowing it."""
    try:
        config = GroupKeyConfig.from_dict(provider_config_to_dict(seed.provider_config))
    except PlanCompileError as exc:
        return f"group_key_config_invalid:{column}:{exc.code}"
    group_by = config.group_by
    if group_by == column:
        return f"group_key_self_anchor_not_native:{column}"
    if source_schema.get_field_index(group_by) < 0:
        return f"group_key_sibling_missing_not_native:{column}:{group_by}"
    if group_by in fk_children or group_by in parent_keys:
        return f"group_key_fk_sibling_not_native:{column}:{group_by}"
    sibling_seed = table_columns.get(group_by)
    if sibling_seed is not None and sibling_seed.strategy != "passthrough":
        return f"group_key_masked_sibling_not_native:{column}:{group_by}"
    sibling_type = source_schema.field(group_by).type
    if not group_key_sibling_type_admitted(sibling_type):
        return f"group_key_sibling_type_not_native:{column}:{group_by}:{sibling_type}"
    return None


def decide_group_key(
    bound: Any | None,
    strategy: str,
    table: str,
    column: str,
    input_type: pa.DataType,
    seed: ColumnSeed,
    source_schema: pa.Schema,
    table_columns: dict[str, ColumnSeed],
    fk_children: set[str],
    parent_keys: set[str],
) -> ColumnDecision:
    """Decide one group_key column, whose native eligibility reads the group_by SIBLING.

    Unlike the per-value ops it cannot use the shared `admit(column, input_type, seed)` (that
    sees only the target), so the sibling-aware reason is resolved here from the source schema.
    `bound` is the already-loaded companion kernel (None when the companion is unavailable), so
    the caller keeps the shared per-strategy load in `SeqNativeRun._kernel`."""
    reason = (
        REASON_UNAVAILABLE
        if bound is None
        else group_key_native_reason(
            column, seed, source_schema, table_columns, fk_children, parent_keys
        )
    )
    native = bound is not None and reason is None
    return ColumnDecision(
        table,
        column,
        RUST_COMPANION if native else PYTHON_SCALAR,
        reason,
        input_type,
        strategy=strategy,
    )


def group_key_column(
    run: SeqNativeRun,
    ctx: StrategyContext,
    df: pd.DataFrame,
    column: str,
    *,
    group_by: str,
    namespace: str,
    mask_key: bytes,
    length: int,
    prefix: str,
) -> list[Any] | None:
    """The group keys for `df`, derived from the SIBLING `df[group_by]`, or None when the Python
    handler runs. Called by `GroupKeyStrategyHandler` through the `ctx.seq_native` seam.

    group_key reads a sibling column, not its own, so it bypasses the per-value
    `_pinned_array`/`apply` path (`run.admitted` still books the row count and gates on the
    `prepare_table` decision). The sibling is stringified exactly as the oracle's
    `to_pandas_fk_safe` frame does (a null-bearing int64 sibling is the nullable `Int64` dtype,
    not float64-widened), then the shared raw-hex derivation produces the key. Output is
    positional, matching the Python handler, so a `when` selection lands on the right rows."""
    gate = run.admitted(ctx, column, GROUP_KEY, len(df))
    if gate is None:
        return None
    decision, bound = gate
    out = derive_group_key_from_strings(
        stringify_group_by_series(df[group_by]),
        length=length,
        prefix=prefix,
        mask_key=mask_key,
        namespace=namespace,
        raw_hex_kernel=bound.kernel,
        native_threads=bound.native_threads,
    )
    decision.native_calls += 1
    return out.to_pylist()
