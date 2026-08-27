"""Schema/config-resolved node requirements for the native planning boundary.

Task 0.2's second half. ``StrategyCapabilities`` (``_capabilities.py``) is
strategy-only and static; ``NodeRequirements`` is what a SPECIFIC compiled
WorkNode needs to run natively, resolved from the node's config plus the source
profile. The split is the whole point: hashing is statically keyed + row-local
regardless of column, but whether a ``date_shift`` node needs a format-detect
prepass depends on whether THAT column's config pins ``date_format``.

Nothing here executes anything or changes behavior. It is a read-only
description a later phase's native executor and the platform's route selector
consult.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution.native._capabilities import (
    StrategyCapabilities,
    capabilities_for,
)

FallbackPolicy = Literal["native", "python_only", "reject_large"]

# Strategies whose output preserves the input column type (no tokenization /
# stringification). Every other static-output strategy emits a string surface
# (a digest, a bucket label, a formatted date, a generalized code).
_TYPE_PRESERVING = frozenset({"passthrough", "shuffle"})

# Strategies that read a durable side table to run natively.
_STATE_TABLE_BY_STRATEGY: dict[str, str] = {
    "code_set": "code_set_corpus",
    "joint_mask": "reference_table",
    "faker": "value_pool",
    "<composite>": "value_pool",
}

# pandas/profile dtype string -> Arrow type. Coarse on purpose: only the
# family matters for whether the output schema is determinate, and the exact
# width is not load-bearing at the planning boundary.
_DTYPE_TO_ARROW: dict[str, pa.DataType] = {
    "int64": pa.int64(),
    "int32": pa.int32(),
    "float64": pa.float64(),
    "float32": pa.float32(),
    "bool": pa.bool_(),
    "boolean": pa.bool_(),
    "object": pa.string(),
    "string": pa.string(),
    "datetime64[ns]": pa.timestamp("ns"),
    "category": pa.string(),
}


@dataclass(frozen=True)
class NodeRequirements:
    """What one compiled WorkNode needs to execute on the native route.

    ``output_arrow_schema is None`` marks an indeterminate output type: the node
    is excluded from the native route (the parity oracle runs it instead).
    """

    required_input_columns: tuple[str, ...]
    output_arrow_schema: pa.Schema | None
    lowering_id: str
    required_prepasses: tuple[str, ...]
    required_state_tables: tuple[str, ...]
    diagnostic_reducers: tuple[str, ...]
    fallback_policy: FallbackPolicy


def _resolve_strategy_name(node: Any) -> str:
    """The capabilities key for a node: its resolved strategy, or the kind
    placeholder for composite / FK-group nodes.

    build_work_list produces only the mask node kinds ("scalar", "composite",
    "composite_fk_group"); generate-table synthesis runs on a separate path and
    never reaches this function today. A generation-context resolution (a
    generate-kind node keyed distinctly from its same-named mask strategy) is
    therefore DEFERRED to the phase that streams generation, rather than left as
    a dead branch here.
    """
    if node.kind == "composite":
        return "<composite>"
    if node.kind == "composite_fk_group":
        return "<group>"
    return node.strategy


def _config_dict(node: Any) -> dict[str, Any]:
    slice_ = node.plan_slice
    provider_config = getattr(slice_, "provider_config", ())
    return provider_config_to_dict(provider_config)


def _required_input_columns(node: Any, cfg: dict[str, Any]) -> tuple[str, ...]:
    cols: list[str] = list(node.columns)
    slice_ = node.plan_slice
    cols.extend(getattr(slice_, "coherent_with", ()) or ())
    # Config-referenced sibling columns a node reads besides its own value.
    for key in ("group_by", "order_by", "anchor", "reference_column"):
        ref = cfg.get(key)
        if isinstance(ref, str) and ref:
            cols.append(ref)
    # Stable de-dup preserving first appearance.
    seen: dict[str, None] = {}
    for c in cols:
        seen.setdefault(c, None)
    return tuple(seen)


def _output_arrow_schema(node: Any, caps: StrategyCapabilities, profile: Any) -> pa.Schema | None:
    if not caps.output_type_is_static:
        return None
    fields: list[pa.Field] = []
    for col in node.columns:
        if node.strategy in _TYPE_PRESERVING:
            arrow_type = _input_arrow_type(node.table, col, profile)
        else:
            # A masked/tokenized/generalized surface is a string.
            arrow_type = pa.string()
        fields.append(pa.field(col, arrow_type))
    return pa.schema(fields)


def _input_arrow_type(table: str, column: str, profile: Any) -> pa.DataType:
    for tbl in getattr(profile, "tables", ()):
        if tbl.name != table:
            continue
        for col in tbl.columns:
            if col.name == column:
                return _DTYPE_TO_ARROW.get(col.dtype, pa.string())
    return pa.string()


def _required_prepasses(
    node: Any, caps: StrategyCapabilities, cfg: dict[str, Any]
) -> tuple[str, ...]:
    passes: list[str] = []
    # date_shift resolves its format from config or a whole-column detect scan.
    if node.strategy == "date_shift" and not cfg.get("date_format"):
        passes.append("format_detect")
    # A durable global row number is needed by permutations, per-group ordinals,
    # and per-row-index seeds (Task 0.1 non-partitionable / global-identity sites).
    if caps.needs_global_row_identity:
        passes.append("global_row_number")
    # A non-row-local global strategy needs a whole-column pass (a percentile
    # bound, an aggregate, a shared-order stream) before any row emits.
    if caps.is_global and not caps.is_row_local:
        passes.append("whole_column_pass")
    seen: dict[str, None] = {}
    for p in passes:
        seen.setdefault(p, None)
    return tuple(seen)


def _diagnostic_reducers(caps: StrategyCapabilities) -> tuple[str, ...]:
    # One per-code globalizer per diagnostic the node can emit. Empty for the
    # zero-diagnostic admitted set, so Phase 1 needs no reducers at all.
    reducers = [f"reduce_warning:{code}" for code in caps.warning_codes]
    reducers += [f"reduce_row_error:{trigger}" for trigger in caps.row_error_modes]
    return tuple(reducers)


def _fallback_policy(caps: StrategyCapabilities) -> FallbackPolicy:
    # `required_prepasses` is intentionally NOT consulted here: a prepass (a
    # date-format detect, a global row number) does not disqualify a node from
    # the native route, it is native work a later phase schedules. So a
    # format-less date_shift is still "native" with a format_detect prepass. The
    # phase that consumes prepasses owns any policy that reads them.
    native_ready = (
        caps.output_type_is_static
        and caps.is_row_local
        and not caps.is_global
        and not caps.needs_global_row_identity
    )
    return "native" if native_ready else "python_only"


def requirements_for(node: Any, *, plan: Any, profile: Any) -> NodeRequirements:
    """Resolve one WorkNode's native execution requirements.

    ``plan`` is accepted for future resolution needs (namespace bindings,
    relationship edges) and is not read for the scalar node kinds today.
    """
    strategy_name = _resolve_strategy_name(node)
    caps = capabilities_for(strategy_name)
    cfg = _config_dict(node)
    state_tables = tuple(t for t in (_STATE_TABLE_BY_STRATEGY.get(strategy_name),) if t is not None)
    return NodeRequirements(
        required_input_columns=_required_input_columns(node, cfg),
        output_arrow_schema=_output_arrow_schema(node, caps, profile),
        lowering_id=f"{node.kind}:{strategy_name}",
        required_prepasses=_required_prepasses(node, caps, cfg),
        required_state_tables=state_tables,
        diagnostic_reducers=_diagnostic_reducers(caps),
        fallback_policy=_fallback_policy(caps),
    )


__all__ = [
    "FallbackPolicy",
    "NodeRequirements",
    "requirements_for",
]
