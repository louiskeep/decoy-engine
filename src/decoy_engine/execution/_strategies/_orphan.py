"""Orphan-FK policy resolution for the pandas execution adapter (engine-v2 S9 slice 2h).

A child FK column references a parent key. Its masked value is the PARENT's
masked value for the same source key, looked up through the in-run parent
source->masked map the runner builds as it masks parents (referential integrity
by construction, not by re-derive coincidence). A child row whose source key has
no parent is an ORPHAN, handled per the edge's `OrphanPolicy` (cross-sprint
contracts row 7; S9 spec 6.2):

- `PRESERVE`: keep the original source key (unmasked).
- `REMAP`: assign a fresh masked key via the parent column's strategy. For most
  strategies this makes the orphan indistinguishable from a normally-masked value.
  For FPE with preserve_separators=True: keys with no in-charset characters (e.g.
  all-uppercase keys like "TERMINATED" or "EMP-ORPHAN") now FAIL CLOSED
  (`FpeUnencryptableError` -> `StrategyError`) -- DE-01 cluster-C (2026-07-14)
  removed the `_covering_hash_to_charset` fallback (fix #42) because its output
  was non-invertible: a column sold as reversible silently did not round-trip.
  An all-out-of-charset orphan key must be masked under a charset that covers it,
  not emitted as a non-recoverable covering hash.
- `WARN`: PRESERVE behavior + one AGGREGATED `QualityWarning(code='orphan_fk')`
  per edge (never one-per-row: a 100k-row child must not emit 100k warnings).
- `FAIL`: raise `ExecutionError(code='orphan_fk_violation')`.

Keys are tuples (a single-column FK is a 1-tuple, a composite FK an N-tuple), so
the same resolver serves scalar FK children and composite-FK group nodes. S9
honors the policy; S10 reports it.

S2 (engine "Finish Open-Ended Surfaces" program, EXCLUDE-then-CASCADE): a
row-errored parent-key row is excluded from `parent_map` by the caller
(`_pandas_adapter.py::_parent_map`), so it can never resolve to its raw value.
A child key that ONLY exists as an excluded (errored) parent key is neither a
normal resolution nor a genuine orphan -- it is a CASCADE: the child row's
masked value becomes `None` (never the raw key) and the caller emits a
synthetic `RowError` on that child row carrying the SAME trigger as the
parent's key-error, so the existing quarantine/fail-loud machinery removes
(covered) or fails loud (uncovered) it uniformly, for every `orphan_policy`.
Precedence per child row: (1) mapped in `parent_map` -> normal resolution;
(2) key present in `errored_parent_keys` -> cascade (masked=None); (3)
otherwise -> genuine orphan, `orphan_policy` applies unchanged. `orphan_policy`
NEVER sees a cascaded key.

Accepted limitation (S2 round 3, when-gated duplicate key): precedence (1) is
a normal resolution only in the sense that it is not a cascade or an orphan;
it does not guarantee a MASKED value. When a `when` gate leaves a parent
FK-key row unmasked and that same raw key value also appears on a different
row that row-errored, precedence (1) resolves the child to the RAW value
carried by the when-gate-unmasked row. This is not a quarantine escape (the
raw value is already present in the parent output because the user's `when`
gate deliberately left it unmasked); net-new exposure is nil. Accepted and
documented, not enforced -- see docs/relationships-memory-scaling.md and the
inline NOTE at the precedence-1 branch below.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import pandas as pd

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import StrategyContext, StrategyHandler
    from decoy_engine.execution._runner import WorkNode

_KeyTuple = tuple[object, ...]
_NodeKey = tuple[str, tuple[str, ...]]


def resolve_fk_keys(
    child_keys: list[_KeyTuple | None],
    parent_map: dict[_KeyTuple, _KeyTuple],
    edge: RelationshipEdge,
    *,
    remap_fn: Callable[[list[_KeyTuple]], list[_KeyTuple]],
    errored_parent_keys: dict[_KeyTuple, str] | None = None,
) -> tuple[list[_KeyTuple | None], list[QualityWarning], list[tuple[int, str]]]:
    """Map each child source key to its masked key, applying the orphan policy.

    `child_keys` carries one entry per child row: `None` for a null FK (preserved
    as null, never an orphan), else the row's source key tuple. Returns the masked
    key per row (None where the input was None), any aggregated warnings, and a
    cascade list of `(row_index, trigger)` for rows whose key was excluded from
    `parent_map` because its parent row was row-errored (S2 EXCLUDE-then-CASCADE).

    `errored_parent_keys` (default None, byte-parity when empty/None) maps a raw
    parent key to the trigger of the row-error that excluded it from `parent_map`.
    Precedence per row: (1) `parent_map` hit -> normal resolution (unchanged); (2)
    key in `errored_parent_keys` -> cascade, masked value is `None` (NEVER the raw
    key), recorded for the caller to emit a `RowError`; (3) otherwise -> genuine
    orphan, `orphan_policy` applies exactly as before. A cascaded key is consumed
    in branch 2 and never reaches the orphan-policy branch.

    CORRECTION (round 3): branch (1) is a "normal resolution" in the sense that
    it is neither a cascade nor an orphan; it is NOT guaranteed to be a masked
    value. A `parent_map` entry can carry a raw when-gate-unmasked value (the
    identity-map contract), so branch (1) can yield raw data. See the accepted
    when-gate limitation NOTE below and docs/relationships-memory-scaling.md.
    """
    masked: list[_KeyTuple | None] = [None] * len(child_keys)
    orphan_positions: list[int] = []
    orphan_keys: list[_KeyTuple] = []
    cascade: list[tuple[int, str]] = []
    for i, key in enumerate(child_keys):
        if key is None:
            continue  # null FK: preserved as null
        mapped = parent_map.get(key)
        if mapped is not None:
            # NOTE (S2 remediation guide r3 section 5, accepted limitation):
            # a parent_map entry is not always a masked value. When a `when`
            # gate leaves a parent FK-key row unmasked, that row's identity
            # entry (raw -> raw) lands here too. If that same raw key value
            # also appears on a different parent row that row-errored, the
            # child resolves via THIS branch to the raw when-gate-unmasked
            # value, not a cascade. This is not a quarantine escape: the raw
            # value is already present in the parent output because the
            # user's own `when` gate deliberately left it unmasked; net-new
            # exposure is nil. Accepted and pinned (do not "fix" without a
            # product decision; see docs/relationships-memory-scaling.md).
            masked[i] = mapped
            continue
        if errored_parent_keys is not None and key in errored_parent_keys:
            # NEVER the raw key; the cascaded RowError removes/fails this row.
            # NOTE (S2 remediation guide section 4): this cascades uniformly
            # under every orphan_policy, including FAIL -- a child of a
            # quarantine-covered parent key error is quarantined (data-quality
            # disposition), not raised as an orphan_fk_violation (RI
            # disposition). If a product owner later decides FAIL must
            # hard-fail here instead, that is a one-line branch
            # (`if edge.orphan_policy is OrphanPolicy.FAIL: raise ...`); do not
            # change this without a product decision (see the guide).
            masked[i] = None
            cascade.append((i, errored_parent_keys[key]))
            continue
        orphan_positions.append(i)
        orphan_keys.append(key)

    if not orphan_positions:
        return masked, [], cascade

    policy = edge.orphan_policy
    if policy is OrphanPolicy.FAIL:
        raise ExecutionError(
            code="orphan_fk_violation",
            message=(
                f"{len(orphan_positions)} orphan row(s) in "
                f"{edge.child_table}.{edge.child_columns} reference no parent key in "
                f"{edge.parent_table}.{edge.parent_columns} (orphan_policy=fail)."
            ),
        )

    if policy is OrphanPolicy.REMAP:
        remapped = remap_fn(orphan_keys)
        for pos, val in zip(orphan_positions, remapped, strict=True):
            masked[pos] = val
        return masked, [], cascade

    # PRESERVE and WARN both keep the source key unmasked.
    for pos, key in zip(orphan_positions, orphan_keys, strict=True):
        masked[pos] = key
    warnings: list[QualityWarning] = []
    if policy is OrphanPolicy.WARN:
        warnings.append(
            QualityWarning(
                code="orphan_fk",
                provider=edge.namespace,
                column=",".join(edge.child_columns),
                detail={
                    "parent_table": edge.parent_table,
                    "parent_columns": list(edge.parent_columns),
                    "child_table": edge.child_table,
                    "child_columns": list(edge.child_columns),
                    "orphan_rows": len(orphan_positions),
                },
            )
        )
    return masked, warnings, cascade


def gather_errored_parent_keys(
    edges: tuple[RelationshipEdge, ...],
    errored_keys_cache: dict[_NodeKey, dict[_KeyTuple, str]] | None,
) -> dict[_KeyTuple, str]:
    """S2: gather the errored parent keys for every edge a child resolves
    against, first-hit-wins (mirrors the parent-map merge order for
    multi-parent children in `_resolve_fk_node`). Extracted from
    `_pandas_adapter.py` to keep that module under the module-size cap (S2
    remediation guide section 6); returns `{}` when `errored_keys_cache` is
    None (byte-parity: `resolve_fk_keys` treats an empty dict the same as
    None via the caller's `or None`)."""
    errored_parent_keys: dict[_KeyTuple, str] = {}
    if errored_keys_cache is not None:
        for edge in edges:
            cache_key: _NodeKey = (edge.parent_table, edge.parent_columns)
            for key, trigger in errored_keys_cache.get(cache_key, {}).items():
                errored_parent_keys.setdefault(key, trigger)
    return errored_parent_keys


def cascade_row_errors(cascade: list[tuple[int, str]], column: str) -> list[RowError]:
    """S2: build one `RowError` per cascaded child row (a row whose FK key
    was excluded from `parent_map` because its parent row was row-errored).
    `column` is the attribution column (a composite FK's quarantine entry
    still carries the whole child row; the column only keys the manifest
    count). Extracted from `_pandas_adapter.py` alongside
    `gather_errored_parent_keys` (S2 remediation guide section 6)."""
    return [
        RowError(
            column=column,
            row_index=pos,
            trigger=trigger,
            reason=(
                "FK parent key was quarantined for a row error; child row "
                "cascaded to quarantine to prevent raw parent-key leak"
            ),
        )
        for pos, trigger in cascade
    ]


def make_remap_fn(
    edge: RelationshipEdge,
    node_by_key: dict[_NodeKey, WorkNode],
    ctx: StrategyContext,
    handlers: dict[str, StrategyHandler],
) -> Callable[[list[_KeyTuple]], list[_KeyTuple]]:
    """A REMAP closure: mask orphan source keys via the PARENT columns' own
    strategies, so a remapped orphan is indistinguishable from a real masked
    value (S9 spec section 6.2 REMAP + Dennis slice-2h brief section G).
    Extracted from `_pandas_adapter.py` (S2 remediation guide r3 section 9,
    LOW pre-emptive extraction to regain module-size headroom); `handlers`
    is the caller's `self._handlers` table, passed in rather than captured
    via `self` since this is now a free function."""
    ptable = edge.parent_table
    pcols = edge.parent_columns

    def remap(orphan_keys: list[_KeyTuple]) -> list[_KeyTuple]:
        if not orphan_keys:
            return []
        masked_cols: list[list[object]] = []
        for j, pcol in enumerate(pcols):
            pnode = node_by_key.get((ptable, (pcol,)))
            if pnode is None or not isinstance(pnode.plan_slice, ColumnSeed):
                raise ExecutionError(
                    code="orphan_remap_parent_missing",
                    message=(
                        f"REMAP needs the parent column {ptable}.{pcol} to be a "
                        "masked scalar node, but it is absent from the work list."
                    ),
                )
            handler = handlers.get(pnode.strategy)
            if handler is None:
                raise ExecutionError(
                    code="unsupported_strategy",
                    message=f"REMAP found no handler for parent strategy {pnode.strategy!r}.",
                )
            tmp = pd.DataFrame({pcol: [k[j] for k in orphan_keys]})
            # Codex P2 MULTI-TABLE EVIDENCE COLLISION remediation: this remaps
            # orphan keys via the PARENT column's own strategy, so any
            # table-identity evidence a handler stamps (CodeSetHandler) must
            # attribute to the parent table, not whatever table a prior
            # `_dispatch_mask_node` call last set. Restored in `finally` since
            # this closure can run interleaved mid-dispatch of the CHILD table.
            prior_table = ctx.current_table
            object.__setattr__(ctx, "current_table", ptable)
            try:
                tmp, _ = handler.run(tmp, pcol, pnode.plan_slice, ctx)
            finally:
                object.__setattr__(ctx, "current_table", prior_table)
            masked_cols.append(list(tmp[pcol]))
        return [tuple(col[i] for col in masked_cols) for i in range(len(orphan_keys))]

    return remap
