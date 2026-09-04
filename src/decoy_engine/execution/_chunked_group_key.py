"""group_key admission + gates for chunked execution (Phase 4 slice 2).

Extracted to keep `_chunked.py` under the orchestration LOC cap, mirroring
`_chunked_dgrn.py` / `_chunked_fk.py`. See `_chunked.py`'s module docstring
for the group_key-admitted-strategy summary and
`docs/plans/2026-08-31-p4-slice2-group-key-chunked.md` for the full design.

`group_key` (`transforms/group_key.apply_group_key`) derives a key from a
SIBLING column's value (`group_by`), not its own: `derive(seed, "group_key/
<col>", str(df[group_by_col][i]).encode())`. Every row sharing a group_by
value gets the identical key, and that computation is per-row pure -- no
whole-column state -- so chunking the rows reproduces the full-frame output
byte-for-byte, PROVIDED the group_by cell each chunk sees is identical to
what the full frame would see at that row. Two correctness properties this
module enforces:

1. `group_key` must stay OUT of `CHUNK_SAFE_STRATEGIES` (`_chunked_fk.py`,
   Trap A). That set is reused verbatim by `_chunked_fk.gate_fk_child_edges`
   as the FK-self-mask allowlist, which assumes every member is keyed on the
   column's OWN value (parent and child compute the same masked bytes from
   the same raw key). A `group_key` FK child would instead derive from its
   own group_by cell under its own column-namespace, which generally
   differs from the parent's, silently breaking referential integrity for
   matched keys. `CHUNK_SIBLING_KEYED_STRATEGIES` is therefore a SEPARATE
   set; `group_key` is admitted into `check_chunked_compatibility`'s
   per-column loop via this set, not by joining `CHUNK_SAFE_STRATEGIES`, so
   it stays correctly rejected by the FK gate exactly as before this slice
   (`chunked_fk_parent_strategy_not_self_mask_safe`, or -- for group_key
   specifically, since it was never chunk-safe to begin with -- would have
   been even before that 2026-09-02 narrowing). Mirrors slice 1's separate
   `CHUNK_DGRN_STRATEGIES`.

2. `group_key` + `when:` must be rejected (Trap D). A when-gated group_key
   masks only matching rows; non-matching rows keep their ORIGINAL value and
   dtype. For a non-string target column, a chunk of all-non-matching rows
   keeps the numeric dtype while a chunk containing matches becomes
   string-typed -- a chunk-boundary-dependent output dtype `concat_masked_
   chunks` correctly refuses (`chunked_schema_mismatch`) where the full-frame
   oracle produces one mixed column. The auto-planner separately rejects ALL
   `when` predicates for auto-routing, but that gate does not run for a
   direct `run_mask_pipeline_chunked` call, so `check_chunked_compatibility`
   -- the public entry point's own gate -- must reject it too
   (`reject_group_key_when`, mirroring `_chunked_dgrn.reject_windowed_date_
   when`).

3. A group_by column whose EFFECTIVE type is unsafe must be rejected (Trap
   E). `apply_group_key`'s `key_cache` keys on the raw Python group_by value,
   but the derivation keys on `str(raw_val)`; two values that are Python
   `==`/hash-equal but stringify differently (the reachable case: a float64
   column holding `0.0` and `-0.0`) collide in the cache, so whichever value
   is seen FIRST in a call wins -- an order a chunk boundary changes,
   breaking byte parity. `group_by_type_is_safe` fixes the safe Arrow-type
   set (floating and decimal excluded); `group_by_effective_type` resolves
   the group_by column's type AT THE POINT the consuming group_key node
   reads it -- the source type, or a preceding sibling mask's STATIC output
   type when that mask is ordered before the group_key node in `order_work`
   (a preceding `when`-gated or dynamic-output mask makes the effective
   domain unprovable, hence unsafe too). `unsafe_group_key_group_by_columns`
   is the shared reason-collector both `reject_unsafe_group_key_group_by_
   dtype` (the manual entrypoint's raising gate) and `_planner._runtime_
   source_rejections` (the auto-route's rejection-string gate) call, so the
   two routes cannot disagree on which group_key columns are admissible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._strategies._text_redact import _DEFAULT_TOKEN
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import ColumnSeed

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

# Admitted via the group_by SIBLING column's value rather than the column's
# own value. See the module docstring (point 1) for why this must stay
# disjoint from CHUNK_SAFE_STRATEGIES.
CHUNK_SIBLING_KEYED_STRATEGIES: frozenset[str] = frozenset({"group_key"})

# Safe Arrow type-check predicates for a group_by column (Trap E). FLOATING
# and DECIMAL are deliberately excluded: both can hold two values that are
# Python `==`/hash-equal but stringify differently (float 0.0/-0.0; decimal
# signed zero or a differing exponent), which collide in `apply_group_key`'s
# key_cache (keyed on the raw value, not `str()`) -- see module docstring
# point 3. Any type not covered here (including decimal, floating, binary,
# list, struct, ...) is unsafe by omission -- fail-closed, not an allowlist
# a new Arrow type could silently join.
_SAFE_GROUP_BY_TYPE_CHECKS = (
    pa.types.is_integer,
    pa.types.is_boolean,
    pa.types.is_string,
    pa.types.is_large_string,
    pa.types.is_date,
    pa.types.is_timestamp,
)


def group_by_type_is_safe(arrow_type: pa.DataType) -> bool:
    """Whether `arrow_type` is safe as a group_key `group_by` source (Trap E).

    Safe: integer, boolean, string, large_string, date, timestamp, or a
    dictionary whose VALUE type is one of those (checked recursively, for a
    dictionary-of-dictionary shape if Arrow can construct one). Unsafe:
    floating, decimal, and anything else -- see `_SAFE_GROUP_BY_TYPE_CHECKS`.
    """
    if pa.types.is_dictionary(arrow_type):
        return group_by_type_is_safe(arrow_type.value_type)
    return any(check(arrow_type) for check in _SAFE_GROUP_BY_TYPE_CHECKS)


def _static_group_by_source_type(node: WorkNode, source_type: pa.DataType) -> pa.DataType | None:
    """The provably-static Arrow output type `node`'s strategy would leave
    on the group_by column, or None when it is DYNAMIC (the column's output
    type depends on cell content, not just config, so it is not knowable
    without seeing the data).

    Scoped EXACTLY to the strategies a group_by column can actually be
    masked by on the chunked route (`CHUNK_SAFE_STRATEGIES` /
    `CHUNK_CONDITIONAL_STRATEGIES` / `dgrn.CHUNK_DGRN_STRATEGIES` /
    `CHUNK_SIBLING_KEYED_STRATEGIES`), plus `formula`/`derived`/`nested` for
    direct unit coverage of Trap E note (i) -- those three are already
    unconditionally rejected by `check_chunked_compatibility`'s general
    strategy-admission loop before this guard would ever see them through
    the real pipeline, so classifying them DYNAMIC here is defensive, not
    reachable, coverage. No existing single map already spans this exact
    set: `out_of_core._mask.masked_output_type` covers the OUT-OF-CORE-
    admitted strategies (hash, truncate, redact, passthrough, fpe,
    text_redact, categorical, plus the Group (c) strategies), which omits
    date_shift/bucketize/top_code/faker/windowed_date entirely --a
    different route with a different admitted-strategy set, not usable
    here without guessing the ones it does not cover. `_mem_estimate_
    schema.py`'s numeric-dtype map is for GENERATE columns, not mask
    output. Per Cam's do-not-guess directive, this map covers exactly what
    a chunked-route group_by column can be masked by, nothing padded in
    from either of those unrelated maps.
    """
    strategy = node.strategy
    cfg = provider_config_to_dict(
        node.plan_slice.provider_config if isinstance(node.plan_slice, ColumnSeed) else ()
    )
    if strategy in ("hash", "fpe", "truncate"):
        return pa.string()
    if strategy == "text_redact":
        # text_redact is a no-op passthrough that keeps the source value + type
        # under EITHER of `TextRedactHandler.run`'s two early returns: a
        # non-string `token` (`_strategies/_text_redact.py:68`) OR a malformed
        # `detectors` that is not None / list / tuple (line 125). provider_config
        # is free-form, so `detectors: 123` survives model_validate and reaches
        # the handler. Mirror BOTH conditions, fail-closed (a superset of the
        # OOC map, which checks the token alone and would miss the detectors
        # case). When text_redact actually runs it stringifies every cell into
        # an object column, so a float 0.0/-0.0 becomes the DISTINCT strings
        # "0.0"/"-0.0" (no cache collision); only a passthrough of an unsafe
        # (float/decimal) source is dangerous, and that is what returning
        # source_type here correctly rejects. Keep in sync with the handler.
        token = cfg.get("token", _DEFAULT_TOKEN)
        detectors = cfg.get("detectors")
        if not isinstance(token, str) or (
            detectors is not None and not isinstance(detectors, (list, tuple))
        ):
            return source_type
        return pa.string()
    if strategy in ("group_key", "windowed_date"):
        # apply_group_key / apply_windowed_date always return list[str]; no
        # null-fallthrough, no content-dependent branch (see each module's
        # own docstring).
        return pa.string()
    if strategy == "redact":
        # Config-aware (the constant replacement value's own inferred
        # type), not data-dependent -- mirrors out_of_core._mask.
        # masked_output_type's identical redact resolution.
        return pa.array([cfg.get("redact_with", "REDACTED")], from_pandas=True).type
    if strategy == "passthrough":
        return source_type
    if strategy == "categorical":
        categories = cfg.get("categories")
        if not categories:
            return None  # from_profile / unresolved: content-dependent.
        return pa.array(categories, from_pandas=True).type
    if strategy == "date_shift":
        # `DateShiftStrategyHandler.run` always formats a parseable cell to
        # a date STRING but keeps an unusable (null / unparseable) cell's
        # ORIGINAL value verbatim. A string source's kept cells are also
        # strings, so the column stays homogeneous string; a non-string
        # source (date32/timestamp) could keep a non-string value for an
        # unparseable cell, mixing types in a way config alone cannot rule
        # out, so that case stays DYNAMIC.
        if pa.types.is_string(source_type) or pa.types.is_large_string(source_type):
            return pa.string()
        return None
    # bucketize / top_code: a non-null-coercible cell falls through to the
    # ORIGINAL value (each module's own docstring: "chunk-content-
    # dependent" / per-cell object-column mixing) -- not knowable from
    # config alone.
    # faker: per-provider output shape has no central declaration.
    # formula / derived / nested: dynamic by construction (see the
    # docstring above; unreachable through the real chunked pipeline).
    return None


def group_by_effective_type(
    ordered_work: list[WorkNode],
    source_schema: pa.Schema,
    *,
    table: str,
    group_key_node: WorkNode,
) -> pa.DataType | None:
    """The Arrow type `group_key_node`'s group_by column effectively has at
    the moment `group_key_node` reads it, or None when that type cannot be
    proven safe (Trap E).

    Work-order aware and PER CONSUMER, not per column: computed relative to
    `group_key_node`'s own position in `ordered_work` (the `order_work`
    output), so two group_key columns sharing one group_by masked BETWEEN
    them each get their own correct answer. Source type unless a masking
    node on the SAME group_by column is ordered strictly before
    `group_key_node`, in which case the effective type is that mask's
    static output type (None if dynamic, or if the mask itself carries a
    `when` predicate -- its passthrough of non-matching rows makes the
    effective domain a MIX of the source type and the mask type, neither
    alone, which is unprovable).
    """
    plan_slice = group_key_node.plan_slice
    if not isinstance(plan_slice, ColumnSeed):
        return None
    group_by = provider_config_to_dict(plan_slice.provider_config).get("group_by")
    if not isinstance(group_by, str) or not group_by:
        return None
    field_index = source_schema.get_field_index(group_by)
    if field_index < 0:
        return None
    source_type = source_schema.field(field_index).type

    consumer_pos = next(
        (pos for pos, node in enumerate(ordered_work) if node.key == group_key_node.key),
        None,
    )
    if consumer_pos is None:
        return source_type

    group_by_key = (table, (group_by,))
    for node in ordered_work[:consumer_pos]:
        if node.table != table:
            continue
        if node.kind == "scalar" and node.key == group_by_key:
            mask_slice = node.plan_slice
            if isinstance(mask_slice, ColumnSeed) and mask_slice.when:
                return None  # Trap E note (ii): mixed effective domain.
            return _static_group_by_source_type(node, source_type)
        if node.kind in ("composite", "composite_fk_group") and group_by in node.columns:
            # A multi-column bundle's output is not a single static type
            # this map resolves; fail closed rather than guess.
            return None
    return source_type


def unsafe_group_key_group_by_columns(
    ordered_work: list[WorkNode],
    source_schema: pa.Schema,
    *,
    table: str,
) -> list[str]:
    """group_key column names on `table` whose group_by EFFECTIVE type is
    not provably safe (Trap E). Empty when every group_key node is safe (or
    `table` has no group_key nodes at all).

    The shared reason-collector: `reject_unsafe_group_key_group_by_dtype`
    (manual entrypoint, raises) and `_planner._runtime_source_rejections`
    (auto route, collects reason strings) both call this so the two routes
    render the identical admission judgment from the identical logic.
    """
    offending: list[str] = []
    for node in ordered_work:
        if node.table != table or node.kind != "scalar" or node.strategy != "group_key":
            continue
        effective = group_by_effective_type(
            ordered_work, source_schema, table=table, group_key_node=node
        )
        if effective is None or not group_by_type_is_safe(effective):
            offending.append(node.columns[0])
    return sorted(offending)


def reject_unsafe_group_key_group_by_dtype(
    plan: Plan,
    source_schema: pa.Schema,
    *,
    table: str,
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
) -> None:
    """Reject `table` when any group_key node's group_by EFFECTIVE type is
    unsafe (Trap E). The manual `run_mask_pipeline_chunked` entrypoint's
    gate; the auto route's equivalent judgment is `unsafe_group_key_group_
    by_columns` called directly from `_planner._runtime_source_rejections`
    (which already has an ordered work list, so it does not go through this
    raising wrapper).

    Raises:
        PlanCompileError: ``code='chunked_group_key_group_by_dtype_unsupported'``.
    """
    from decoy_engine.execution._runner import build_work_list, order_work

    ordered_work = order_work(build_work_list(plan, registry), relationship_graph)
    offending = unsafe_group_key_group_by_columns(ordered_work, source_schema, table=table)
    if not offending:
        return
    raise PlanCompileError(
        code="chunked_group_key_group_by_dtype_unsupported",
        path=f"tables.{table}.columns",
        message=(
            f"group_key column(s) {', '.join(offending)} on table {table!r} read a "
            "group_by value whose effective type cannot be proven safe for chunked "
            "self-masking: floating and decimal group_by values can hold two "
            "==/hash-equal values with different str() results (e.g. float 0.0 / "
            "-0.0), which collide in apply_group_key's per-call key_cache and make "
            "the FIRST-SEEN value's key win for every later colliding value -- an "
            "order a chunk boundary changes. A dynamic-output or when-gated "
            "preceding sibling mask on the group_by column is equally unprovable. "
            "Use run_pipeline or run_sequential instead, or change the group_by "
            "column's (effective) type to integer, boolean, string, date, or "
            "timestamp."
        ),
    )


def reject_group_key_when(table_cfg: dict[str, Any], *, table: str) -> None:
    """Reject a `group_key` column that also carries a `when:` predicate (Trap D).

    Raises:
        PlanCompileError: ``code='chunked_group_key_when_not_supported'``.
    """
    when_cols = sorted(
        str(col_entry.get("name", "?"))
        for col_entry in table_cfg.get("columns") or []
        if isinstance(col_entry, dict)
        and col_entry.get("strategy") == "group_key"
        and col_entry.get("when")
    )
    if not when_cols:
        return
    raise PlanCompileError(
        code="chunked_group_key_when_not_supported",
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(when_cols)} combine 'group_key' with a 'when:' "
            "predicate, which is not supported on the chunked route: a when-gated "
            "group_key passes non-matching rows through with their ORIGINAL value "
            "and dtype, so a chunk of all-non-matching rows keeps the source dtype "
            "while a chunk containing matches becomes string-typed -- a "
            "chunk-boundary-dependent output dtype the full-frame oracle does not "
            "produce as two separate types."
        ),
    )
