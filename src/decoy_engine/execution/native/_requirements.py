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

import re
from dataclasses import dataclass
from typing import Any, Literal

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution.native._capabilities import (
    StrategyCapabilities,
    capabilities_for,
)
from decoy_engine.plan._checks_truncate import check_truncate_config
from decoy_engine.plan._errors import PlanCompileError

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
    # Every signed/unsigned integer width the Rust kernel's `is_admitted_type`
    # accepts, in both the numpy label (int8) and the pandas nullable label
    # (Int8); both convert to the same Arrow int type, so omitting a width would
    # over-reject a column the native hash kernel can process, breaking the
    # required correspondence with `is_admitted_type`.
    "int8": pa.int8(),
    "int16": pa.int16(),
    "int32": pa.int32(),
    "int64": pa.int64(),
    "uint8": pa.uint8(),
    "uint16": pa.uint16(),
    "uint32": pa.uint32(),
    "uint64": pa.uint64(),
    "Int8": pa.int8(),
    "Int16": pa.int16(),
    "Int32": pa.int32(),
    "Int64": pa.int64(),
    "UInt8": pa.uint8(),
    "UInt16": pa.uint16(),
    "UInt32": pa.uint32(),
    "UInt64": pa.uint64(),
    "float64": pa.float64(),
    "float32": pa.float32(),
    "bool": pa.bool_(),
    "boolean": pa.bool_(),
    "object": pa.string(),
    "string": pa.string(),
    "datetime64[ns]": pa.timestamp("ns"),
    "category": pa.string(),
}

# canonical_dtype_label (internal.pandas_compat) always normalizes a tz-aware
# datetime column to "datetime64[ns, <tz>]" regardless of source resolution,
# so this is the one pattern needed to recover the zone for pa.timestamp.
_TZ_DATETIME_LABEL = re.compile(r"^datetime64\[ns, (?P<tz>.+)\]$")

# The native-hash kernel's admitted input set (decoy-engine-native/src/
# canonicalize.rs::is_admitted_type), mirrored exactly: a value the compiled
# derive_batch can canonicalize without falling back to the pure-Python
# per-value path. Float and a tz-naive timestamp are NOT admitted; a
# tz-AWARE timestamp is (any unit).
_ADMITTED_NATIVE_HASH_TYPES = (
    pa.utf8(),
    pa.large_utf8(),
    pa.bool_(),
    pa.int8(),
    pa.int16(),
    pa.int32(),
    pa.int64(),
    pa.uint8(),
    pa.uint16(),
    pa.uint32(),
    pa.uint64(),
)


def is_admitted_native_hash_type(arrow_type: pa.DataType) -> bool:
    """Whether `arrow_type` is in the compiled hash kernel's admitted set."""
    if pa.types.is_timestamp(arrow_type):
        return arrow_type.tz is not None
    return arrow_type in _ADMITTED_NATIVE_HASH_TYPES


# The exact set of mask strategies with a compiled native kernel this phase:
# `_kernels_scalar.py` (native_passthrough, native_redact, native_truncate)
# and `_kernels_keyed.py` (native_keyed_hash). A strategy can have
# native-FRIENDLY static capabilities (row-local, static output type) with no
# kernel built for it yet (fpe, date_shift, bucketize, ...); capabilities
# alone cannot tell the two apart, so this allowlist is the single source of
# truth for "a kernel actually exists to run this." Task 2.7's dispatch must
# route through this SAME constant rather than recompute its own admitted
# set, or eligibility and dispatch could silently diverge on which
# strategies have a kernel. Grows only when a later task lands a new kernel.
NATIVE_KERNEL_STRATEGIES = frozenset({"passthrough", "redact", "truncate", "hash"})


def native_kernel_rejection(name: str, strategy: str) -> str | None:
    """The coded reason `strategy` has no compiled native kernel this phase,
    or None when it does.

    The `<composite>` / `<group>` node-kind placeholders are exempt: their
    admission is governed by their own `StrategyCapabilities` entry (a
    composite bundle or an FK-group node has no single per-strategy kernel
    to look up), not by this per-mask-strategy allowlist.
    """
    if strategy in ("<composite>", "<group>"):
        return None
    if strategy in NATIVE_KERNEL_STRATEGIES:
        return None
    return f"no_native_kernel:{name}:{strategy}"


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


def resolve_input_arrow_type(table: str, column: str, profile: Any) -> pa.DataType | None:
    """The Arrow type `column` resolves to from the profile, or None when it
    cannot be resolved (the column is not in the profile, or its dtype label
    is not one `_DTYPE_TO_ARROW` / the tz-aware datetime pattern recognizes).

    None is the "unknowable" signal a caller must not paper over with a
    default: `_input_arrow_type` below still defaults to `pa.string()` for the
    type-preserving output-schema use (a determinate schema is required
    there and string is the safe universal fallback), but a caller deciding
    whether a value is admissible to the native hash kernel must treat None
    as "cannot prove this is safe," not as "assume Utf8."
    """
    for tbl in getattr(profile, "tables", ()):
        if tbl.name != table:
            continue
        for col in tbl.columns:
            if col.name != column:
                continue
            if col.dtype in _DTYPE_TO_ARROW:
                return _DTYPE_TO_ARROW[col.dtype]
            tz_match = _TZ_DATETIME_LABEL.match(col.dtype)
            if tz_match:
                return pa.timestamp("ns", tz=tz_match.group("tz"))
            return None
    return None


def _input_arrow_type(table: str, column: str, profile: Any) -> pa.DataType:
    return resolve_input_arrow_type(table, column, profile) or pa.string()


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


def hash_config_rejection(name: str, table: str, profile: Any | None) -> str | None:
    """The coded reason a `hash` column's resolved input type cannot run on the
    compiled hash kernel, or None when it can.

    Without a profile the input type cannot be resolved at all; this returns
    None (deferred, not admitted-by-default) rather than guessing, matching
    `native_route_eligibility`'s documented profile-optional boundary.

    `mixed_object_not_native` fires for a dtype label `resolve_input_arrow_type`
    does not recognize at all (it returns None) -- e.g. `timedelta64[ns]` or an
    explicit `large_string[pyarrow]`. These are rejected to the oracle rather
    than defaulted to Utf8, which conservatively over-rejects some safe types
    (a narrowing, never a widening). It does NOT catch a genuinely mixed-content
    pandas ``object`` column: the profiler's coarse label reports that as
    ``object`` too, which maps to string and is admitted; the execution-time
    Arrow conversion is the backstop there, since the coarse label cannot tell
    it from a plain string column.
    """
    if profile is None:
        return None
    resolved = resolve_input_arrow_type(table, name, profile)
    if resolved is None:
        return f"mixed_object_not_native:{name}"
    if not is_admitted_native_hash_type(resolved):
        return f"hash_input_type_not_native:{name}:{resolved!s}"
    return None


def truncate_config_rejection(name: str, provider_config: dict[str, Any]) -> str | None:
    """The coded reason a `truncate` config would fail `TruncateHandler`'s
    fail-closed checks, or None when it would not.

    Runs the actual plan-compile check (`check_truncate_config`) over a
    single-column config built from `provider_config`, so this and the
    compiler apply the identical length/keep/mask_char rules (including the
    legacy `from_end` -> `keep` resolution) rather than a re-typed copy that
    could drift from it.
    """
    synthetic_config = {
        "tables": [
            {
                "name": "_",
                "columns": [
                    {"name": name, "strategy": "truncate", "provider_config": provider_config}
                ],
            }
        ]
    }
    try:
        check_truncate_config(synthetic_config)
    except PlanCompileError as exc:
        return f"{exc.code}:{name}"
    return None


def redact_config_rejection(name: str, provider_config: dict[str, Any]) -> str | None:
    """The coded reason a `redact` config cannot run on the native kernel, or
    None when it can.

    The native contract is string-only (`native_redact` pins its output to
    `pa.string()` for a string `redact_with`, which is what every shipped
    disguise uses); a non-string `redact_with` makes the output type
    data-dependent instead of static, so it is rejected here before it can
    reach the native route.
    """
    redact_with = provider_config.get("redact_with", "REDACTED")
    if not isinstance(redact_with, str):
        return f"redact_with_not_string:{name}"
    return None


def _config_gate_rejection(
    node: Any, strategy_name: str, cfg: dict[str, Any], profile: Any
) -> str | None:
    """Dispatch to the config/type gate for `strategy_name`, or None for a
    strategy that has none. Shared with `native_route_eligibility` via the
    same three functions above, so the compiler and the eligibility query
    can never reach a different verdict for the same column."""
    if not node.columns:
        return None
    name = node.columns[0]
    if strategy_name == "hash":
        return hash_config_rejection(name, node.table, profile)
    if strategy_name == "truncate":
        return truncate_config_rejection(name, cfg)
    if strategy_name == "redact":
        return redact_config_rejection(name, cfg)
    return None


def _fallback_policy(
    caps: StrategyCapabilities, config_reason: str | None, kernel_reason: str | None
) -> FallbackPolicy:
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
        and config_reason is None
        and kernel_reason is None
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
    column_name = node.columns[0] if node.columns else "?"
    kernel_reason = native_kernel_rejection(column_name, strategy_name)
    config_reason = _config_gate_rejection(node, strategy_name, cfg, profile)
    state_tables = tuple(t for t in (_STATE_TABLE_BY_STRATEGY.get(strategy_name),) if t is not None)
    return NodeRequirements(
        required_input_columns=_required_input_columns(node, cfg),
        output_arrow_schema=_output_arrow_schema(node, caps, profile),
        lowering_id=f"{node.kind}:{strategy_name}",
        required_prepasses=_required_prepasses(node, caps, cfg),
        required_state_tables=state_tables,
        diagnostic_reducers=_diagnostic_reducers(caps),
        fallback_policy=_fallback_policy(caps, config_reason, kernel_reason),
    )


__all__ = [
    "NATIVE_KERNEL_STRATEGIES",
    "FallbackPolicy",
    "NodeRequirements",
    "hash_config_rejection",
    "is_admitted_native_hash_type",
    "native_kernel_rejection",
    "redact_config_rejection",
    "requirements_for",
    "resolve_input_arrow_type",
    "truncate_config_rejection",
]
