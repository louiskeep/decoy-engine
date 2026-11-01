"""DuckDB EXPLAIN-plan and optimizer helpers for `StreamFkJoiner` (moved out
of `_stream_join.py`): the pinned-optimizer pragma, the slim unordered join
query, EXPLAIN (FORMAT JSON) parsing, and the unordered-plan verification.

`StreamFkJoiner._iter_unordered_join_rows` runs the join with no `ORDER BY` (so
the never-OOM claim rests on the join's build side staying on the bounded
parent, not on DuckDB's own unbounded global sort) and calls
`_verify_unordered_plan_or_raise` on every real drain, not just a dedicated
test, to fail closed if a future optimizer change reintroduces a global sort
or flips the build side onto the unsized child stream. No behavior change from
the move; see `_stream_join.py`'s module docstring for the joiner's full
lifecycle.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._join import _q

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


# DuckDB's optimizers, by name in `duckdb_optimizers()`, pinned off so the
# unordered join keeps the WRITTEN join order (child probe, deduplicated
# parent build) rather than DuckDB's own cardinality-estimate-driven choice:
# `build_side_probe_side` can swap which side builds the hash table, and
# `join_order` can reorder multi-relation joins. Kept as a module constant so
# the structural plan-pin test can assert on the exact same names this module
# pragmas against.
_PINNED_DISABLED_OPTIMIZERS = ("build_side_probe_side", "join_order")


def _is_global_sort_operator(name: str) -> bool:
    """True for any physical-plan operator that performs a GLOBAL sort/order.

    A SUBSTRING match ("SORT"/"ORDER"), plus the sort-limit `TOP_N`, rather than
    a fixed name blacklist: the whole point of `run_ordered_join`'s
    unordered-join + bounded-sorter split is that NEITHER join input re-injects a
    global sort, so an operator the pinned DuckDB does not currently emit (or one
    a future version renames, e.g. a bare `SORT`) must still fail closed here, not
    slip through an exact-name list. Our real unordered plan is a `HASH_JOIN`
    over scans/projections/filters -- none of which contain "SORT"/"ORDER" -- so
    this never false-positives on a correct plan (and a sort-merge join, which
    would, is already rejected by the single-`HASH_JOIN` requirement)."""
    upper = name.upper()
    return "SORT" in upper or "ORDER" in upper or upper == "TOP_N"


def _disable_join_optimizers(conn: Any) -> None:
    """Pin the joiner's own connection to the written join order (parent-build).

    Public DuckDB API only (`duckdb_optimizers()` + `SET disabled_optimizers`),
    not a library customization. Appends to any `disabled_optimizers` value
    `connect_duckdb` may already carry rather than overwriting it, since this
    connection is per-joiner and not shared with the resident/relation-build
    connections that never register an unsized Arrow stream as a join side.
    """
    names = {row[0] for row in conn.execute("SELECT name FROM duckdb_optimizers()").fetchall()}
    existing = conn.execute("SELECT current_setting('disabled_optimizers')").fetchone()[0]
    disabled = {name for name in existing.split(",") if name}
    for optimizer in _PINNED_DISABLED_OPTIMIZERS:
        if optimizer in names:
            disabled.add(optimizer)
        # else: an older/renamed DuckDB without this optimizer name never
        # swaps/reorders this way; a missing name must degrade to a no-op,
        # never an error.
    if disabled:
        conn.execute(f"SET disabled_optimizers = '{','.join(sorted(disabled))}'")


def _read_disabled_optimizers(conn: Any) -> frozenset[str]:
    value = conn.execute("SELECT current_setting('disabled_optimizers')").fetchone()[0]
    return frozenset(name for name in value.split(",") if name)


def _unordered_join_query(join_key: str, masked_key_columns: Sequence[str]) -> str:
    """The SLIM unordered join query -- shared by `_iter_unordered_join_rows`
    and `explain_join()` so the plan a test inspects is exactly the plan the
    drain actually runs.

    Projects ONLY what the bounded sorter must reorder: `__decoy_row_nr` (the
    sort key), a COMPACT nullable-boolean match token (`TRUE` matched, NULL
    orphan -- never the raw parent value), and the masked parent components.
    The raw child columns (`__decoy_fk_join_key`, `__decoy_src_*`) stay OUT of
    the SELECT -- they are re-fetched out-of-line from `SpillChildKeys` in
    phase 3 -- so no wide raw key, orphan, or dictionary encoding can overflow
    the sorter. `__decoy_fk_join_key` remains the JOIN predicate, not output.
    """
    select_list = [
        f"c.{_q('__decoy_row_nr')}",
        f"CASE WHEN p.{_q(join_key)} IS NULL THEN NULL ELSE TRUE END "
        f"AS {_q('__decoy_parent_match')}",
    ]
    for idx, masked_column in enumerate(masked_key_columns):
        select_list.append(f"p.{_q(masked_column)} AS {_q(f'__decoy_parent_masked_{idx}')}")
    # The statement text keeps the indentation it had as a method body; the
    # characterization test pins it byte for byte.
    return f"""
            SELECT {", ".join(select_list)}
            FROM child_keys c
            LEFT JOIN parent_keys p
              ON c.__decoy_fk_join_key = p.{_q(join_key)}
        """


def _explain_physical_plan(conn: Any, query: str) -> dict[str, Any]:
    """Parsed `EXPLAIN (FORMAT JSON)` physical plan for `query`.

    Assumes `child_keys` is already registered on `conn` (the caller's job,
    since a fresh reader must back it and EXPLAIN never consumes that reader --
    it plans without executing). Fails closed
    (`out_of_core_fk_join_plan_unverified`) on any malformed, missing, or
    unparseable EXPLAIN output rather than proceeding on an unverified plan.
    """
    rows = conn.execute(f"EXPLAIN (FORMAT JSON) {query}").fetchall()
    # Iterate WITHOUT a 2-tuple unpack in the comprehension: a DuckDB version
    # that changes EXPLAIN's result arity would otherwise raise a bare
    # ValueError with no `code`, escaping this function's coded fail-closed
    # contract (the parity harness would see an unexpected crash, not an
    # admitted rejection). A non-2-column row simply does not match.
    raw = next(
        (row[1] for row in rows if len(row) == 2 and row[0] == "physical_plan"),
        None,
    )
    if raw is None:
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_FK_JOIN_PLAN_UNVERIFIED,
            message="EXPLAIN (FORMAT JSON) returned no 'physical_plan' row.",
        )
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_FK_JOIN_PLAN_UNVERIFIED,
            message="EXPLAIN (FORMAT JSON) output was not valid JSON.",
        ) from exc
    if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_FK_JOIN_PLAN_UNVERIFIED,
            message=(f"EXPLAIN (FORMAT JSON) plan had an unexpected top-level shape: {parsed!r}."),
        )
    return parsed[0]


def _iter_plan_nodes(node: Any) -> Iterator[dict[str, Any]]:
    """Depth-first walk of a parsed `EXPLAIN (FORMAT JSON)` operator tree.

    Fails closed on any node that is not a `{"name": ..., "children": [...]}`
    shape: a malformed plan must never be silently skipped over, since a
    verification that quietly ignores what it cannot parse is no verification
    at all.
    """
    if not isinstance(node, dict) or "name" not in node:
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_FK_JOIN_PLAN_UNVERIFIED,
            message=(
                "the unordered join's EXPLAIN (FORMAT JSON) plan had an unexpected "
                f"node shape (expected a dict with a 'name' key, got {node!r}); "
                "refusing to run an unverified plan."
            ),
        )
    yield node
    children = node.get("children", [])
    if not isinstance(children, list):
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_FK_JOIN_PLAN_UNVERIFIED,
            message=(
                f"the unordered join's EXPLAIN plan node {node.get('name')!r} had a "
                "non-list 'children'; refusing to run an unverified plan."
            ),
        )
    for child in children:
        yield from _iter_plan_nodes(child)


def _verify_unordered_plan_or_raise(plan: dict[str, Any]) -> None:
    """Fail-closed structural guard (Codex plan-gate MEDIUM 4).

    The unordered join's physical plan must carry NO global sort operator (the
    whole reason it is unordered is that order is restored separately, by the
    bounded sorter), and its single `HASH_JOIN` must keep the WRITTEN join
    order: the child (a registered Arrow reader, `ARROW_SCAN`) as the streamed
    probe side, and the deduplicated parent (a `read_parquet` view,
    `READ_PARQUET`) as the build side. This is a structural check on operator
    TYPES, not the query's view aliases: a future optimizer change that
    reintroduces a global sort, or flips the build side onto the (unsized,
    unbounded) child stream, would make the join hold an O(child) resident
    hash table -- exactly the regression the pinned optimizers exist to
    prevent -- so it must fail closed here, not silently regress.
    """
    nodes = list(_iter_plan_nodes(plan))
    for node in nodes:
        if _is_global_sort_operator(node["name"]):
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_FK_JOIN_PLAN_UNVERIFIED,
                message=(
                    f"the unordered join's plan regained a global sort operator "
                    f"({node['name']!r}); the reorder path requires an UNORDERED "
                    "join -- order is restored separately by the bounded sorter."
                ),
            )
    joins = [node for node in nodes if node["name"] == "HASH_JOIN"]
    if len(joins) != 1:
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_FK_JOIN_PLAN_UNVERIFIED,
            message=(
                "expected exactly one HASH_JOIN operator in the unordered join's "
                f"plan, found {len(joins)}; refusing to run an unverified plan."
            ),
        )
    join = joins[0]
    children = join.get("children", [])
    if len(children) != 2:
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_FK_JOIN_PLAN_UNVERIFIED,
            message=(
                f"the HASH_JOIN operator had {len(children)} children, expected "
                "exactly 2 (probe, build); refusing to run an unverified plan."
            ),
        )
    probe, build = children
    join_type = join.get("extra_info", {}).get("Join Type")
    # Match the SCAN LEAVES of each join input, not the immediate child operator:
    # a future DuckDB planner that interposes a PROJECTION/FILTER (e.g. for the
    # `AS __decoy_parent_masked_i` projections) between the join and its scans
    # would otherwise fail-close a correct plan. Descending to the leaf scans
    # keeps the O(child)-build detection (the child stream is ARROW, the parent
    # relation is READ_PARQUET) while tolerating benign interposed operators. A
    # flipped build still fails: the flip swaps which subtree holds which scan.
    probe_scans = _subtree_scan_names(probe)
    build_scans = _subtree_scan_names(build)
    probe_has_child = any("ARROW" in name for name in probe_scans)
    probe_has_parent = any("PARQUET" in name for name in probe_scans)
    build_has_child = any("ARROW" in name for name in build_scans)
    build_has_parent = any("PARQUET" in name for name in build_scans)
    # EXCLUSIVE placement, not mere presence: the child ARROW stream must be on
    # the PROBE side and NOT the build side, and the parent PARQUET relation on
    # the BUILD side and NOT the probe. A build subtree that contains ANY child
    # scan builds an O(child) hash table even if it also reads the parent, so
    # `build_has_child` (or `probe_has_parent`) fails closed -- checking only for
    # a parent scan somewhere in the build subtree would let that through.
    plan_ok = (
        join_type == "LEFT"
        and probe_has_child
        and not probe_has_parent
        and build_has_parent
        and not build_has_child
    )
    if not plan_ok:
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_FK_JOIN_PLAN_UNVERIFIED,
            message=(
                "the unordered join's physical plan did not keep the written join "
                f"order (child ARROW probe / deduplicated-parent PARQUET build): "
                f"Join Type={join_type!r}, probe scans={sorted(probe_scans)!r}, "
                f"build scans={sorted(build_scans)!r}. A regained global sort or a "
                "flipped build side would make the join hold an O(child) resident "
                "hash table, breaking the never-OOM memory model; refusing to run "
                "an unverified plan."
            ),
        )


def _subtree_scan_names(node: dict[str, Any]) -> set[str]:
    """Uppercased operator names of the SCAN LEAVES under `node` (a leaf has no
    children). The plan tree is already shape-validated by `_iter_plan_nodes`."""
    children = node.get("children", [])
    if not children:
        return {str(node.get("name", "")).upper()}
    names: set[str] = set()
    for child in children:
        names |= _subtree_scan_names(child)
    return names
