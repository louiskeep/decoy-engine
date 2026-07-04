"""SS3: the closure engine. THE novel core of Sprint G.

Pattern: semi-naive Datalog fixpoint evaluation (Ullman, "Principles of
Database and Knowledge-Base Systems", ch. 3; equivalently the Kleene /
Knaster-Tarski monotone fixpoint over a finite powerset lattice, the same
class of algorithm as the worklist algorithm in monotone dataflow
frameworks). Each rule application is a relational semi-join; termination
follows from monotonicity + finiteness, NOT from graph acyclicity -- so
cycles need no special casing beyond the no-growth exit. Tonic/Redgate-style
subsetters describe the same two rules as "downstream/upstream traversal".

The two rules, in pure key-set terms, for edge `e` (parent P with columns
`pc`, child C with columns `cc`):

- Downward (cascade): a C-row whose FK key equals a surviving P-row's key
  survives.
- Upward (parent completeness): a P-row whose key equals a NON-NULL FK key
  of a surviving C-row survives.

Null semantics match `validation.post._checks._fk_validity` exactly: a
child key tuple with any null component is null; a null key never matches
downward (polars join default `join_nulls=False`, verified) and never
demands an upward pull (`drop_nulls()` before the upward semi-join). A
non-null child key absent from the ENTIRE parent table is a source orphan;
preflight already counted and gated it (`_preflight.py` section 5.4).

Multi-parent edges are traversed independently (see `_edges.py`'s module
docstring): a shared child key pulls matching rows from EVERY parent table
that has it, which is why this module iterates `edges` rather than grouping
by child.

This module performs NO file I/O: it consumes in-memory key frames
(`_keys.py`'s output) and produces row-index sets. `RelationshipGraph.ordering`
(the mask-time column-node topological order) is deliberately NOT used here
-- it is not a closure traversal order (verified: it is a column-node walk,
and table-level cycles never appear in it as cycles); the fixpoint loop below
is the correct closure order for both acyclic and cyclic schemas.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import polars as pl

from decoy_engine.subset._errors import SubsetInternalError
from decoy_engine.subset._keys import RI
from decoy_engine.subset._types import (
    ClosureResult,
    EdgeDirection,
    EdgeStats,
    RoundTrace,
    SubsetEdge,
)

BudgetCheck = Callable[[Mapping[str, set[int]], tuple[EdgeStats, ...]], None]


def _downward_step(
    e: SubsetEdge, survivors: dict[str, set[int]], key_frames: Mapping[str, pl.DataFrame]
) -> set[int]:
    pkf, ckf = key_frames[e.parent_table], key_frames[e.child_table]
    if not survivors[e.parent_table]:
        return set()
    surv_keys = (
        pkf.filter(pl.col(RI).is_in(sorted(survivors[e.parent_table])))
        .select(list(e.parent_columns))
        .unique()
    )
    # Null child keys never match (polars default join_nulls=False): consistent
    # with the FK-validity null semantics this module mirrors.
    matched = ckf.join(
        surv_keys, left_on=list(e.child_columns), right_on=list(e.parent_columns), how="semi"
    )
    return set(matched[RI].to_list()) - survivors[e.child_table]


def _upward_step(
    e: SubsetEdge, survivors: dict[str, set[int]], key_frames: Mapping[str, pl.DataFrame]
) -> set[int]:
    pkf, ckf = key_frames[e.parent_table], key_frames[e.child_table]
    if not survivors[e.child_table]:
        return set()
    needed = (
        ckf.filter(pl.col(RI).is_in(sorted(survivors[e.child_table])))
        .select(list(e.child_columns))
        .drop_nulls()  # any-null component = null key; a null FK demands no upward pull.
        .unique()
    )
    matched = pkf.join(
        needed, left_on=list(e.parent_columns), right_on=list(e.child_columns), how="semi"
    )
    return set(matched[RI].to_list()) - survivors[e.parent_table]


def _stats(
    edges: tuple[SubsetEdge, ...],
    directions: Mapping[str, EdgeDirection],
    down_added: Mapping[str, int],
    up_added: Mapping[str, int],
) -> tuple[EdgeStats, ...]:
    return tuple(
        EdgeStats(
            edge_id=e.edge_id,
            direction=directions[e.edge_id],
            rows_added_downward=down_added[e.edge_id],
            rows_added_upward=up_added[e.edge_id],
        )
        for e in edges
    )


def compute_closure(
    *,
    edges: tuple[SubsetEdge, ...],
    directions: Mapping[str, EdgeDirection],
    key_frames: Mapping[str, pl.DataFrame],
    seed_rows: Mapping[str, frozenset[int]],
    budget_check: BudgetCheck,
) -> ClosureResult:
    """Run the downward/upward fixpoint to closure. See module docstring for the algorithm.

    Terminates the first round that adds zero rows across every table (the
    ONLY exit path -- `terminated_by` is always "fixpoint" so tests assert
    the exit condition, not merely that the function returned).

    Termination argument (implementation guide section 4.5): `survivors` only
    grows (`new - survivors[...]` then union), so the state space is the
    finite product of per-table row-set powersets; the strictly-increasing
    chain of survivor states has length at most `N = sum(rows(t))`. No step
    in this argument mentions acyclicity, so self-referencing and mutually
    cyclic edges terminate the same way as acyclic ones: a row already in
    `survivors` can never be re-added, so a cycle cannot re-enqueue work.
    `max_rounds` below is a defensive-only assertion for an engine bug (e.g.
    a future non-monotone step); it is never the exit path on correct input.
    """
    tables = sorted(key_frames)
    survivors: dict[str, set[int]] = {t: set(seed_rows.get(t, frozenset())) for t in tables}
    down_added = {e.edge_id: 0 for e in edges}
    up_added = {e.edge_id: 0 for e in edges}
    trace: list[RoundTrace] = []

    max_rounds = sum(kf.height for kf in key_frames.values()) + 2
    rounds = 0
    while True:
        rounds += 1
        if rounds > max_rounds:
            raise SubsetInternalError(
                code="subset_closure_nontermination",
                message="closure exceeded its monotone growth bound; this is an engine bug, "
                "report it",
            )
        round_added_per_table: dict[str, int] = {}

        for e in edges:  # fixed sorted order (from _edges.py): byte-determinism.
            d = directions[e.edge_id]
            if d in ("both", "downward"):
                new = _downward_step(e, survivors, key_frames)
                if new:
                    survivors[e.child_table] |= new
                    down_added[e.edge_id] += len(new)
                    round_added_per_table[e.child_table] = round_added_per_table.get(
                        e.child_table, 0
                    ) + len(new)
            if d in ("both", "upward"):
                new = _upward_step(e, survivors, key_frames)
                if new:
                    survivors[e.parent_table] |= new
                    up_added[e.edge_id] += len(new)
                    round_added_per_table[e.parent_table] = round_added_per_table.get(
                        e.parent_table, 0
                    ) + len(new)

        total_added = sum(round_added_per_table.values())
        trace.append(RoundTrace(rounds, total_added, tuple(sorted(round_added_per_table.items()))))
        edge_stats = _stats(edges, directions, down_added, up_added)
        # Budget gate runs at the end of every round: early abort, exact
        # attribution to the edge that pushed a table/total over cap, and no
        # cheaper correct alternative (a mid-round check would misattribute
        # an overshoot to whichever edge happened to run last).
        budget_check(survivors, edge_stats)
        if total_added == 0:  # the no-growth exit; the ONLY exit.
            break

    return ClosureResult(
        survivors={t: frozenset(s) for t, s in survivors.items()},
        rounds=rounds,
        terminated_by="fixpoint",
        edge_stats=_stats(edges, directions, down_added, up_added),
        trace=tuple(trace),
    )


def verify_closure(
    *,
    edges: tuple[SubsetEdge, ...],
    directions: Mapping[str, EdgeDirection],
    key_frames: Mapping[str, pl.DataFrame],
    result: ClosureResult,
) -> None:
    """Defensive re-check of the upward-completeness invariant.

    For every upward-enabled edge, every surviving child row's non-null FK
    key must be EITHER present among surviving parent rows' keys OR absent
    from the entire parent table (a source orphan, already counted at
    preflight). This is the last line of defense for the sprint's core risk
    (a missed upward pull = a dangling FK): cheap (key frames are already in
    memory), and independent of the fixpoint loop's own bookkeeping.
    """
    for e in edges:
        if directions[e.edge_id] not in ("both", "upward"):
            continue
        pkf, ckf = key_frames[e.parent_table], key_frames[e.child_table]
        surviving_child_keys = (
            ckf.filter(pl.col(RI).is_in(sorted(result.survivors[e.child_table])))
            .select(list(e.child_columns))
            .drop_nulls()
            .unique()
        )
        surviving_parent_keys = pkf.filter(
            pl.col(RI).is_in(sorted(result.survivors[e.parent_table]))
        ).select(list(e.parent_columns))
        unmatched = surviving_child_keys.join(
            surviving_parent_keys,
            left_on=list(e.child_columns),
            right_on=list(e.parent_columns),
            how="anti",
        )
        if unmatched.height == 0:
            continue
        # Every unmatched key must be absent from the FULL parent table (a
        # source orphan) -- otherwise the closure missed an upward pull.
        full_parent_keys = pkf.select(list(e.parent_columns)).drop_nulls().unique()
        still_present = unmatched.join(
            full_parent_keys,
            left_on=list(e.child_columns),
            right_on=list(e.parent_columns),
            how="semi",
        )
        if still_present.height > 0:
            raise SubsetInternalError(
                code="subset_closure_invariant_violated",
                message=f"edge {e.edge_id}: {still_present.height} surviving child key(s) "
                "reference a parent key that exists in the source parent table but was not "
                "pulled into the surviving parent set. This is a closure engine bug.",
            )
