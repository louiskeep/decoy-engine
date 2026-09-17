"""Orphan-FK REMAP minting for the pandas execution adapter (engine-v2 S9 slice 2h).

Task 4.6 slice 5b-ii moved this module's policy-resolution core
(`resolve_fk_keys`, `gather_errored_parent_keys`, `cascade_row_errors`) to
`execution/_fk_resolve.py`, a parent-level module both this adapter and the
shadow coordinator's generate-parent FK seam (`execution/physical/
_shadow_fk.py`) import unchanged -- one owner of the FK map + orphan
precedence. `make_remap_fn` below stays here: it is pandas/adapter-bound
(builds a pandas `DataFrame`, dispatches through `StrategyContext`/
`StrategyHandler`), with no shadow-side equivalent to share it with.

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

from typing import TYPE_CHECKING

import pandas as pd

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_resolve import (
    cascade_row_errors,
    gather_errored_parent_keys,
    resolve_fk_keys,
)
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.relationships._graph import RelationshipEdge

if TYPE_CHECKING:
    from collections.abc import Callable

    from decoy_engine.execution._adapter import StrategyContext, StrategyHandler
    from decoy_engine.execution._runner import WorkNode

_KeyTuple = tuple[object, ...]
_NodeKey = tuple[str, tuple[str, ...]]

# Re-exported for every caller that imported the policy-resolution core from
# this module before the Task 4.6 slice 5b-ii extraction (its real home is
# now `execution/_fk_resolve.py`; see the module docstring).
__all__ = [
    "cascade_row_errors",
    "gather_errored_parent_keys",
    "make_remap_fn",
    "resolve_fk_keys",
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
