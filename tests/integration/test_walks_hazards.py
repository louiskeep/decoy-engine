"""Tests for the six walks hazard detectors.

Each test builds a `SchemaSnapshot` in-memory shaped to trigger one
hazard kind, runs `detect_hazards`, and asserts the right hazard fires
with the right details. Modeled on the chaser stress-test schema
(forge-platform/plans/chaser-stress-test-schema.sql) which has every
hazard kind labeled in commentary.

No real DB needed — these are pure-function tests.
"""

from __future__ import annotations

from decoy_engine.walks import (
    Column,
    Edge,
    SchemaSnapshot,
    Table,
    detect_hazards,
)


def _col(name: str, *, nullable: bool = True, pk: bool = False, dtype: str = "integer") -> Column:
    """Test fixture helper — saves typing."""
    return Column(name=name, data_type=dtype, nullable=nullable, is_primary_key=pk)


def _table(name: str, columns: list[Column]) -> Table:
    return Table(name=name, schema="public", columns=tuple(columns))


# ── HUB ──────────────────────────────────────────────────────────────


def test_hub_fires_when_in_degree_above_threshold():
    """A table referenced by 6 others crosses the HUB threshold (5)."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table("users", [_col("id", pk=True, nullable=False)]),
            *[
                _table(
                    f"feature_{i}",
                    [
                        _col("id", pk=True, nullable=False),
                        _col("user_id", nullable=False),
                    ],
                )
                for i in range(6)
            ],
        ),
        declared_edges=tuple(
            Edge(f"feature_{i}", "user_id", "users", "id", declared=True) for i in range(6)
        ),
    )
    hazards = detect_hazards(snap)
    hub = [h for h in hazards if h.kind == "HUB"]
    assert len(hub) == 1
    assert hub[0].table == "users"
    assert hub[0].details["incoming_edge_count"] == 6


def test_hub_does_not_fire_below_threshold():
    """3 incoming FKs is below the HUB threshold of 5; no hazard."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table("users", [_col("id", pk=True, nullable=False)]),
            *[
                _table(
                    f"feature_{i}",
                    [
                        _col("id", pk=True, nullable=False),
                        _col("user_id", nullable=False),
                    ],
                )
                for i in range(3)
            ],
        ),
        declared_edges=tuple(
            Edge(f"feature_{i}", "user_id", "users", "id", declared=True) for i in range(3)
        ),
    )
    hazards = detect_hazards(snap)
    assert not [h for h in hazards if h.kind == "HUB"]


# ── SR (self-reference) ──────────────────────────────────────────────


def test_self_reference_fires_for_parent_id_column():
    """Classic parent/child tree pattern — `teams.parent_team_id`
    points back at `teams.id`."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table(
                "teams",
                [
                    _col("id", pk=True, nullable=False),
                    _col("parent_team_id", nullable=True),
                ],
            ),
        ),
        declared_edges=(Edge("teams", "parent_team_id", "teams", "id", declared=True),),
    )
    hazards = detect_hazards(snap)
    sr = [h for h in hazards if h.kind == "SR"]
    assert len(sr) == 1
    assert sr[0].table == "teams"
    assert "parent_team_id" in sr[0].description


# ── PE (parallel edges) ──────────────────────────────────────────────


def test_parallel_edges_fires_when_multiple_fks_to_same_target():
    """`issues` has 4 FKs to `users` (assignee/reporter/created_by/resolved_by) —
    classic chaser stress-test pattern."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table("users", [_col("id", pk=True, nullable=False)]),
            _table(
                "issues",
                [
                    _col("id", pk=True, nullable=False),
                    _col("assignee_id", nullable=True),
                    _col("reporter_id", nullable=True),
                    _col("created_by_id", nullable=False),
                    _col("resolved_by_id", nullable=True),
                ],
            ),
        ),
        declared_edges=(
            Edge("issues", "assignee_id", "users", "id", declared=True),
            Edge("issues", "reporter_id", "users", "id", declared=True),
            Edge("issues", "created_by_id", "users", "id", declared=True),
            Edge("issues", "resolved_by_id", "users", "id", declared=True),
        ),
    )
    hazards = detect_hazards(snap)
    pe = [h for h in hazards if h.kind == "PE"]
    assert len(pe) == 1
    assert pe[0].table == "issues"
    assert pe[0].details["target_table"] == "users"
    assert sorted(pe[0].details["source_columns"]) == [
        "assignee_id",
        "created_by_id",
        "reporter_id",
        "resolved_by_id",
    ]


def test_parallel_edges_excludes_self_reference():
    """Multiple SRs (e.g. `comments.parent_id` + `comments.thread_id`) shouldn't
    fire as PE — SR has its own kind. PE is for cross-table parallel."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table(
                "comments",
                [
                    _col("id", pk=True, nullable=False),
                    _col("parent_id", nullable=True),
                    _col("thread_id", nullable=True),
                ],
            ),
        ),
        declared_edges=(
            Edge("comments", "parent_id", "comments", "id", declared=True),
            Edge("comments", "thread_id", "comments", "id", declared=True),
        ),
    )
    hazards = detect_hazards(snap)
    pe = [h for h in hazards if h.kind == "PE"]
    sr = [h for h in hazards if h.kind == "SR"]
    assert pe == []
    assert len(sr) == 2  # one per self-FK column


# ── PM (polymorphic FK) ──────────────────────────────────────────────


def test_polymorphic_fk_fires_on_type_id_pattern():
    """`comments.entity_id` + `comments.entity_type` with no FK on
    entity_id → polymorphic. Classic Rails-style "commentable" pattern."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table(
                "comments",
                [
                    _col("id", pk=True, nullable=False),
                    _col("entity_type", dtype="varchar", nullable=False),
                    _col("entity_id", nullable=False),
                ],
            ),
        ),
        declared_edges=(),
    )
    hazards = detect_hazards(snap)
    pm = [h for h in hazards if h.kind == "PM"]
    assert len(pm) == 1
    assert pm[0].table == "comments"
    assert pm[0].details == {
        "type_column": "entity_type",
        "id_column": "entity_id",
    }


def test_polymorphic_fk_does_not_fire_when_id_has_declared_fk():
    """If the `_id` column has a real FK, the table isn't actually
    polymorphic — the type column might just be a denormalized
    convenience field."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table("posts", [_col("id", pk=True, nullable=False)]),
            _table(
                "comments",
                [
                    _col("id", pk=True, nullable=False),
                    _col("post_type", dtype="varchar", nullable=False),
                    _col("post_id", nullable=False),
                ],
            ),
        ),
        declared_edges=(Edge("comments", "post_id", "posts", "id", declared=True),),
    )
    hazards = detect_hazards(snap)
    assert not [h for h in hazards if h.kind == "PM"]


# ── ALT (alternative parents) ────────────────────────────────────────


def test_alt_fires_for_two_nullable_fks_to_different_parents():
    """`labels` table can belong to either an organization or a project
    (XOR via CHECK constraint at the DB level). Detected by the
    "two nullable FKs to different parents" heuristic."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table("organizations", [_col("id", pk=True, nullable=False)]),
            _table("projects", [_col("id", pk=True, nullable=False)]),
            _table(
                "labels",
                [
                    _col("id", pk=True, nullable=False),
                    _col("name", dtype="varchar", nullable=False),
                    _col("organization_id", nullable=True),
                    _col("project_id", nullable=True),
                ],
            ),
        ),
        declared_edges=(
            Edge("labels", "organization_id", "organizations", "id", declared=True),
            Edge("labels", "project_id", "projects", "id", declared=True),
        ),
    )
    hazards = detect_hazards(snap)
    alt = [h for h in hazards if h.kind == "ALT"]
    assert len(alt) == 1
    assert alt[0].table == "labels"
    assert sorted(alt[0].details["parent_tables"]) == ["organizations", "projects"]


# ── CIR (cycles) ─────────────────────────────────────────────────────


def test_cycle_fires_for_three_table_loop():
    """Classic chaser pattern: workflows → statuses → status_transitions
    → workflows. The detector should catch this and report once."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table(
                "workflows",
                [
                    _col("id", pk=True, nullable=False),
                    _col("start_status_id", nullable=True),
                ],
            ),
            _table(
                "statuses",
                [
                    _col("id", pk=True, nullable=False),
                    _col("transition_id", nullable=True),
                ],
            ),
            _table(
                "status_transitions",
                [
                    _col("id", pk=True, nullable=False),
                    _col("workflow_id", nullable=False),
                ],
            ),
        ),
        declared_edges=(
            Edge("workflows", "start_status_id", "statuses", "id", declared=True),
            Edge("statuses", "transition_id", "status_transitions", "id", declared=True),
            Edge("status_transitions", "workflow_id", "workflows", "id", declared=True),
        ),
    )
    hazards = detect_hazards(snap)
    cir = [h for h in hazards if h.kind == "CIR"]
    assert len(cir) == 1
    cycle = cir[0].details["cycle"]
    # Canonicalized to start at lexicographically smallest table.
    assert cycle[0] == "status_transitions"
    assert set(cycle) == {"workflows", "statuses", "status_transitions"}


def test_cycle_does_not_double_count_same_loop_via_different_starting_points():
    """The DFS visits every node — but the cycle should only be reported
    once because we canonicalize on the smallest table name."""
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(
            _table("a", [_col("id", pk=True, nullable=False), _col("b_id", nullable=True)]),
            _table("b", [_col("id", pk=True, nullable=False), _col("a_id", nullable=True)]),
        ),
        declared_edges=(
            Edge("a", "b_id", "b", "id", declared=True),
            Edge("b", "a_id", "a", "id", declared=True),
        ),
    )
    hazards = detect_hazards(snap)
    cir = [h for h in hazards if h.kind == "CIR"]
    assert len(cir) == 1
    # Self-reference detector also fires? No — these are cross-table.
    assert not [h for h in hazards if h.kind == "SR"]


# ── compose: empty schema → no hazards ───────────────────────────────


def test_empty_schema_produces_no_hazards():
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=(),
        declared_edges=(),
    )
    assert detect_hazards(snap) == ()


# QA walks/generators F4 (2026-06-01, HIGH reliability) ─────────────────


def test_walks_gen_f4_deep_chain_does_not_recursionerror():
    """F4 contract: chain of 1500 tables A0 -> A1 -> ... -> A1499 must
    detect no cycles AND must not raise RecursionError. Python's
    default recursion limit is 1000; the iterative DFS rewrite lifts
    the cap to the stack-size limit (effectively unbounded for
    schema-shape walks)."""
    depth = 1500
    tables = tuple(
        _table(
            f"t{i:04d}",
            [
                _col("id", pk=True, nullable=False),
                _col("parent_id", nullable=True) if i > 0 else _col("self_marker", nullable=True),
            ],
        )
        for i in range(depth)
    )
    edges = tuple(
        Edge(f"t{i:04d}", "parent_id", f"t{i - 1:04d}", "id", declared=True)
        for i in range(1, depth)
    )
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=tables,
        declared_edges=edges,
    )
    hazards = detect_hazards(snap)
    cir = [h for h in hazards if h.kind == "CIR"]
    assert cir == [], (
        f"QA walks/generators F4: 1500-deep chain has no cycles. Got {len(cir)} CIR hazards."
    )


def test_walks_gen_f4_deep_cycle_detected_without_recursionerror():
    """F4 contract: a 1200-deep cycle (A0 -> A1 -> ... -> A1199 -> A0)
    must be detected as exactly one CIR hazard. Pre-fix the recursive
    DFS hit RecursionError on the back-edge insertion path."""
    depth = 1200
    tables = tuple(
        _table(
            f"t{i:04d}",
            [
                _col("id", pk=True, nullable=False),
                _col("next_id", nullable=False),
            ],
        )
        for i in range(depth)
    )
    edges = tuple(
        Edge(f"t{i:04d}", "next_id", f"t{(i + 1) % depth:04d}", "id", declared=True)
        for i in range(depth)
    )
    snap = SchemaSnapshot(
        db_kind="postgres",
        schema_name="public",
        tables=tables,
        declared_edges=edges,
    )
    hazards = detect_hazards(snap)
    cir = [h for h in hazards if h.kind == "CIR"]
    assert len(cir) == 1, (
        f"QA walks/generators F4: 1200-deep cycle must produce exactly "
        f"one CIR hazard. Got {len(cir)}."
    )
    cycle = cir[0].details["cycle"]
    assert len(cycle) == depth
    # Canonical cycle starts at the lexicographically smallest table.
    assert cycle[0] == "t0000"
