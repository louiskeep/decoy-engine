"""Multi-parent FK on the out-of-core route: one first-hit-wins parent relation per child tuple.

A child column tuple may declare FK edges to several parent tables (the polymorphic
association). The pandas oracle resolves it by merging the parents' source-to-masked maps
with `dict.setdefault` in `graph.edges` order (`_pandas_adapter._resolve_fk_node`): the first
parent holding a key wins, a row is an orphan only when no parent holds it, and REMAP mints
through the first edge's parent. The out-of-core route joins a child against ONE deduplicated
parent relation per edge, so N parents would mean N joins with the later one overwriting; this
module instead folds the N member relations into one relation with exactly the oracle's
precedence, and the child is then joined as a single-parent child by the unchanged joiners.

Pattern: the priority union. `UNION ALL` the members tagged with their edge rank, keep the
minimum rank per join key, join back for that row's masked columns: the standard SQL
rendering of a COALESCE-in-declared-order lookup over several lookup tables. The three-step
STAGE / `min(rank) GROUP BY key` / JOIN-BACK form is `_relation._build_relation`'s, kept for
the same reason it was chosen there: a `min` over an integer has fixed-size aggregate state
that DuckDB's external hash aggregate spills cleanly, and landing the winners to disk between
the two statements keeps the aggregate and the join from ever being co-resident blocking
operators. Nothing here re-implements a join, a sort or spill; DuckDB owns them.

The members' masked key columns must carry identical Arrow types. The oracle's merged column
is typed from its values (pandas), which a fixed-schema streaming route cannot reproduce byte
for byte, so a type mismatch is refused with the sanctioned `out_of_core_fk_key_dtype_unsupported`
before any output exists; the job runs on the sequential route instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import pyarrow.parquet as pq

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import FK_KEY_DTYPE_UNSUPPORTED_CODE
from decoy_engine.execution.out_of_core._duckdb import connect_duckdb
from decoy_engine.execution.out_of_core._join import _sql_string
from decoy_engine.execution.out_of_core._relation import ParentKeyRelation
from decoy_engine.execution.out_of_core._scratch import ArenaLike, edge_work_dir

if TYPE_CHECKING:
    from pathlib import Path

    from decoy_engine.relationships._graph import RelationshipEdge

_RANK = "__decoy_edge_rank"


def resolve_incoming(
    incoming_edges: Sequence[RelationshipEdge],
    parent_relations: dict[RelationshipEdge, ParentKeyRelation],
    *,
    temp_dir: Path,
    memory_limit: str | None,
    arena: ArenaLike | None = None,
) -> tuple[RelationshipEdge, ...]:
    """One joinable edge per child tuple, its relation bound in `parent_relations`.

    Edges sharing `child_columns` are one multi-parent group, in the order they arrive
    (`graph.edges` order, the oracle's precedence). The group's first edge represents it:
    `parent_relations[first]` is rebound to the merged relation, so every downstream
    consumer (REMAP seeds, the WARN and FAIL identity, the per-column component map) reads
    the first parent exactly as the oracle does, and the other members' entries are removed
    together with their files, which no later table reads (a relation is keyed by its edge,
    and each edge serves one child). A singleton group passes through untouched. A group whose
    edges disagree on the orphan policy is refused with the planner's own code,
    `orphan_policy_conflict`, before anything is merged.
    """
    groups: dict[tuple[str, ...], list[RelationshipEdge]] = {}
    for edge in incoming_edges:
        groups.setdefault(edge.child_columns, []).append(edge)
    representatives: list[RelationshipEdge] = []
    merged_count = 0
    for members in groups.values():
        first = members[0]
        representatives.append(first)
        if len(members) == 1:
            continue
        policies = {edge.orphan_policy for edge in members}
        if len(policies) > 1:
            # The planner rejects this at graph build (`orphan_policy_conflict`); a graph
            # that bypassed it must not be resolved under the first edge's policy silently.
            raise ExecutionError(
                code="orphan_policy_conflict",
                message=(
                    f"out-of-core multi-parent FK: the edges into {first.child_table}."
                    f"{first.child_columns} declare different orphan policies "
                    f"({sorted(p.value for p in policies)}); one policy per child tuple is "
                    "required, as the planner enforces at graph build."
                ),
            )
        member_relations = tuple(parent_relations[edge] for edge in members)
        parent_relations[first] = merge_parent_relations(
            member_relations,
            temp_dir=temp_dir / f"group_{merged_count}",
            memory_limit=memory_limit,
            arena=arena,
        )
        merged_count += 1
        for edge, relation in zip(members, member_relations, strict=True):
            if edge is not first:
                del parent_relations[edge]
            # The merged copy supersedes the member on disk; keep the spill footprint to one.
            relation.path.unlink(missing_ok=True)
    return tuple(representatives)


def merge_parent_relations(
    relations: Sequence[ParentKeyRelation],
    *,
    temp_dir: Path,
    memory_limit: str | None,
    arena: ArenaLike | None = None,
) -> ParentKeyRelation:
    """Fold member relations (in precedence order) into one first-hit-wins relation."""
    if len(relations) == 1:
        return relations[0]
    first = relations[0]
    masked_columns = first.masked_key_columns
    first_types = _masked_types(first)
    for relation in relations[1:]:
        if relation.masked_key_columns != masked_columns:
            raise AssertionError("multi-parent members must share one child tuple arity")
        types = _masked_types(relation)
        if types != first_types:
            raise ExecutionError(
                code=FK_KEY_DTYPE_UNSUPPORTED_CODE,
                message=(
                    "out-of-core multi-parent FK: the parents' masked key types differ "
                    f"({', '.join(map(str, first_types))} vs {', '.join(map(str, types))}); "
                    "a fixed output schema cannot reproduce the oracle's value-typed merged "
                    "column byte for byte, so the job is rejected rather than allowed to drift. "
                    "Run it with execution_mode='sequential'."
                ),
            )
    # T04 (plan 1.3): the merged relation output and its staged/winners COPY
    # scratch plus DuckDB's spill live inside one registered native directory,
    # allocated before any write (the attempt arena's, or the adopted temp_dir);
    # the layout and file names are unchanged, so the merge stays byte-identical.
    temp_dir = edge_work_dir(arena, temp_dir, writer_id="multi-parent")
    # Scratch lives under the same `duckdb/` subtree as the connection's spill, exactly as
    # `_relation._build_relation` lays out its own staged and winners files.
    duckdb_dir = temp_dir / "duckdb"
    duckdb_dir.mkdir(parents=True, exist_ok=True)
    out_path = temp_dir / "merged_key_relation.parquet"
    staged_path = duckdb_dir / "merged_key_staged.parquet"
    winners_path = duckdb_dir / "merged_key_winners.parquet"
    columns = ", ".join([first.join_key_column, *masked_columns])
    members_sql = " UNION ALL ".join(
        f"SELECT {rank} AS {_RANK}, {columns} FROM read_parquet({_sql_string(str(r.path))})"
        for rank, r in enumerate(relations)
    )
    conn = connect_duckdb(temp_dir=duckdb_dir, memory_limit=memory_limit)
    try:
        conn.execute(f"COPY ({members_sql}) TO {_sql_string(str(staged_path))} (FORMAT PARQUET)")
        staged_sql = f"read_parquet({_sql_string(str(staged_path))})"
        conn.execute(
            f"""
            COPY (
                SELECT {first.join_key_column}, min({_RANK}) AS __decoy_win_rank
                FROM {staged_sql}
                GROUP BY {first.join_key_column}
            ) TO {_sql_string(str(winners_path))} (FORMAT PARQUET)
            """
        )
        masked_select = ", ".join(f"s.{col} AS {col}" for col in masked_columns)
        conn.execute(
            f"""
            COPY (
                SELECT s.{first.join_key_column}, {masked_select}
                FROM {staged_sql} s
                JOIN read_parquet({_sql_string(str(winners_path))}) w
                  ON s.{first.join_key_column} = w.{first.join_key_column}
                 AND s.{_RANK} = w.__decoy_win_rank
            ) TO ? (FORMAT PARQUET)
            """,
            [str(out_path)],
        )
    except BaseException:
        # COPY is not atomic; never leave a partial relation behind.
        out_path.unlink(missing_ok=True)
        raise
    finally:
        try:
            conn.close()
        finally:
            try:
                staged_path.unlink(missing_ok=True)
            finally:
                winners_path.unlink(missing_ok=True)
    return ParentKeyRelation(
        path=out_path,
        join_key_column=first.join_key_column,
        masked_key_columns=masked_columns,
        max_sort_payload_row_bytes=max(r.max_sort_payload_row_bytes for r in relations),
    )


def _masked_types(relation: ParentKeyRelation) -> tuple[str, ...]:
    schema = pq.read_metadata(relation.path).schema.to_arrow_schema()
    return tuple(str(schema.field(name).type) for name in relation.masked_key_columns)


__all__ = ["merge_parent_relations", "resolve_incoming"]
