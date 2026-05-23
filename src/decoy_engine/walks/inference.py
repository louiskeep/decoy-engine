"""Heuristic PK/FK inference.

`information_schema` declared FKs are authoritative. This module fills
in the gaps: warehouses where the DBA didn't declare constraints
(common in Snowflake / Redshift / BigQuery deployments) but the column
naming follows a `*_id` -> `<table>.id` convention.

The output is `Edge` objects with `declared=False`. Downstream code
(graph, hazards, UI) uses that flag to label inferred edges as
best-guess rather than ground truth.

Conservative on purpose. False positives here propagate to chase /
walk results; surfacing a wrong edge is worse than missing one. The
heuristic only fires when:

  1. A column ends in `_id` (case-insensitive), and
  2. Stripping `_id` produces a name that matches a table in the
     snapshot (singular-or-plural tolerant — `customer_id` matches
     both `customer` and `customers`), and
  3. That target table has a column named `id`, and
  4. No declared edge from the same `(source_table, source_column)`
     to that target already exists.

Anything more aggressive needs data inspection (e.g. value-overlap
sampling), which is Phase 3.
"""
from __future__ import annotations

from decoy_engine.walks.types import Edge, SchemaSnapshot, Table


def infer_edges(snapshot: SchemaSnapshot) -> tuple[Edge, ...]:
    """Return the heuristic-only edges. Caller is expected to merge
    these with `snapshot.declared_edges` (typically via `build_er_graph`,
    which does the merge for you).

    Order is stable: edges are returned in (source_table, source_column,
    target_table) order so test assertions don't depend on dict
    iteration order.
    """
    by_name: dict[str, Table] = {t.name: t for t in snapshot.tables}

    declared_pairs: set[tuple[str, str]] = {
        (e.source_table, e.source_column) for e in snapshot.declared_edges
    }

    inferred: list[Edge] = []
    for table in snapshot.tables:
        for col in table.columns:
            target_name = _candidate_target(col.name, by_name)
            if target_name is None:
                continue
            target_table = by_name[target_name]
            if not _has_id_pk(target_table):
                continue
            # Don't shadow a declared FK on the same column.
            if (table.name, col.name) in declared_pairs:
                continue
            # Self-reference is allowed — the SR detector picks them up.
            inferred.append(
                Edge(
                    source_table=table.name,
                    source_column=col.name,
                    target_table=target_name,
                    target_column="id",
                    declared=False,
                )
            )

    inferred.sort(
        key=lambda e: (e.source_table, e.source_column, e.target_table)
    )
    return tuple(inferred)


def _candidate_target(column_name: str, by_name: dict[str, Table]) -> str | None:
    """If `column_name` looks like a FK to some table in the snapshot,
    return that table's name. Else None.

    Match priority (most specific first):

      1. Full stem matches a table singular: `customer_id` -> `customer`
      2. Full stem matches a table plural:   `customer_id` -> `customers`
      3. Last component matches a table singular: `parent_team_id` -> `team`
      4. Last component matches a table plural:   `parent_team_id` -> `teams`

    Steps 3+4 catch the common "qualifier + basename + _id" pattern —
    `parent_team_id`, `manager_user_id`, `from_status_id`. Without them
    self-references with qualifiers wouldn't infer.
    """
    if not column_name.lower().endswith("_id"):
        return None
    stem = column_name[:-3]  # strip the "_id"
    if not stem:
        return None

    # Full stem (singular then plural). Avoids false-pluralizing
    # something like `address_id` -> `addresses` when an `address`
    # table also exists.
    if stem in by_name:
        return stem
    if stem + "s" in by_name:
        return stem + "s"

    # Fallback: the last underscore-separated component. Lets
    # `parent_team_id` find `teams` even when no `parent_team` table
    # exists.
    if "_" in stem:
        last = stem.rsplit("_", 1)[-1]
        if last in by_name:
            return last
        if last + "s" in by_name:
            return last + "s"

    return None


def _has_id_pk(table: Table) -> bool:
    """True iff the table has a column named `id` that's the PK.

    Tightening the heuristic to require PK status (not just an `id`
    column) avoids spurious edges to columns named `id` that aren't
    actually keys.
    """
    return any(col.name.lower() == "id" and col.is_primary_key for col in table.columns)
