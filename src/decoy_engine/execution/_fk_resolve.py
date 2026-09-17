"""Parent-level FK orphan-policy resolution helpers (Task 4.6 slice 5b-ii).

`resolve_fk_keys`, `gather_errored_parent_keys`, and `cascade_row_errors` used
to live in `execution/_strategies/_orphan.py`. They moved here, unchanged,
because a MASK-child column whose FK parent is a GENERATE table needs the
identical map-hit/cascade/orphan-policy precedence the pandas oracle applies
(`_pandas_adapter.py::_resolve_fk_node`) -- but the shadow coordinator that
builds a generate-parent's identity map (`execution/physical/_shadow_fk.py`)
must never import `execution._strategies` (a pandas-adapter-only package) or
pull `execution.physical` into `_strategies` the other way. Sitting at this
PARENT `execution` level, both `_pandas_adapter.py` and `execution/physical/
_shadow_fk.py` import the SAME functions -- one owner of the FK map + orphan
precedence, so the two implementations cannot drift (Codex 5b-ii plan-gate
correction #1).

`make_remap_fn` (`_strategies/_orphan.py`) stays where it is: it builds a
pandas `DataFrame` and dispatches through `StrategyContext`/`StrategyHandler`,
genuinely pandas/adapter-bound, with no shadow-side equivalent to share it
with (a generate parent's REMAP orphan is always a coded rejection on the
shadow side -- see `_shadow_fk.py`'s own remap closure). `_parent_map`
(`_pandas_adapter.py`) also stays: it owns source snapshots, parent-masking
state, and key-error exclusions the shadow side's generate-parent identity
map has no equivalent for either.

This move is behavior-preserving by construction (verbatim function bodies);
`tests/unit/execution/test_de10_fk_lossless_typing.py`'s and this module's own
pinned regression test assert `run_pipeline`'s FK output is byte-identical
before and after the extraction.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

if TYPE_CHECKING:
    pass

_KeyTuple = tuple[object, ...]
_NodeKey = tuple[str, tuple[str, ...]]

__all__ = ["cascade_row_errors", "gather_errored_parent_keys", "resolve_fk_keys"]


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
